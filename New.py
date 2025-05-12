from flask_mail import Mail, Message
import os
import logging
import json
import threading
import time
import re
from jinja2 import Environment
import zlib
from datetime import datetime
from flask import Flask, render_template, request, flash, redirect, url_for, session, send_from_directory, make_response
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, BooleanField, SubmitField, RadioField
from wtforms.validators import DataRequired, Email, Optional, ValidationError
from flask_session import Session
from flask_caching import Cache
from tenacity import retry, stop_after_attempt, wait_exponential
import pandas as pd
import plotly.express as px
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.service_account import Credentials
import gspread
from dotenv import load_dotenv
import traceback
from werkzeug.routing import BuildError
import random

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('app.log')]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')
mail = Mail(app)
if not app.config['SECRET_KEY']:
    logger.critical("FLASK_SECRET_KEY not set. Application will not start.")
    raise RuntimeError("FLASK_SECRET_KEY environment variable not set.")

# Enable the zip filter in Jinja2
def zip_filter(*args, **kwargs):
    return zip(*args, **kwargs)

app.jinja_env.filters['zip'] = zip_filter

# Validate critical environment variables
required_env_vars = ['SMTP_SERVER', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD', 'SPREADSHEET_ID', 'GOOGLE_CREDENTIALS_JSON']
for var in required_env_vars:
    if not os.getenv(var):
        logger.critical(f"{var} not set. Application will not start.")
        raise RuntimeError(f"{var} environment variable not set.")

# Configure server-side session
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(app.root_path, 'flask_session')
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hour
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_COOKIE_NAME'] = 'session_id'
app.config['SESSION_COOKIE_SECURE'] = False  # Set to False for local testing
os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)

# Session backup directory
SESSION_BACKUP_DIR = os.path.join(app.root_path, 'session_backup')
os.makedirs(SESSION_BACKUP_DIR, exist_ok=True)

# Custom session interface for compression with backup
from flask.sessions import SessionInterface, SecureCookieSession

class CompressedSession(SessionInterface):
    def open_session(self, app, request):
        session_data = request.cookies.get(self.get_cookie_name(app))
        if not session_data:
            logger.info("No session cookie found, creating new session")
            return SecureCookieSession()
        try:
            compressed_data = bytes.fromhex(session_data)
            decompressed_data = zlib.decompress(compressed_data).decode('utf-8')
            session = SecureCookieSession(json.loads(decompressed_data))
            logger.debug(f"Session loaded: {session}")
            # Attempt to restore from backup if session is empty but email exists
            if 'budget_data' not in session and session.get('email'):
                session = self.restore_from_backup(session.get('email'), session)
            return session
        except Exception as e:
            logger.error(f"Error decompressing session data: {e}")
            return SecureCookieSession()

    def save_session(self, app, session, response):
        if not session.modified:
            logger.debug("Session not modified, skipping save")
            return
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        if not session:
            if self.get_cookie_name(app) in request.cookies:
                response.delete_cookie(
                    self.get_cookie_name(app), domain=domain, path=path
                )
                logger.info("Empty session, deleted cookie")
            return
        try:
            session_data = json.dumps(dict(session)).encode('utf-8')
            compressed_data = zlib.compress(session_data)
            encoded_data = compressed_data.hex()
            response.set_cookie(
                self.get_cookie_name(app),
                encoded_data,
                max_age=app.permanent_session_lifetime,
                secure=app.config['SESSION_COOKIE_SECURE'],
                httponly=self.get_cookie_httponly(app),
                samesite=self.get_cookie_samesite(app),
                domain=domain,
                path=path
            )
            logger.debug(f"Session saved: {dict(session)}")
            # Backup session data if email exists
            if session.get('budget_data', {}).get('email'):
                self.backup_session(session.get('budget_data', {}).get('email'), session)
        except Exception as e:
            logger.error(f"Error saving session: {e}")

    def backup_session(self, email, session):
        try:
            backup_file = os.path.join(SESSION_BACKUP_DIR, f"{sanitize_input(email)}.json")
            with open(backup_file, 'w') as f:
                json.dump(dict(session), f)
            logger.info(f"Session backed up for {email}")
        except Exception as e:
            logger.error(f"Failed to backup session for {email}: {e}")

    def restore_from_backup(self, email, session):
        try:
            backup_file = os.path.join(SESSION_BACKUP_DIR, f"{sanitize_input(email)}.json")
            if os.path.exists(backup_file):
                with open(backup_file, 'r') as f:
                    backup_data = json.load(f)
                session.update(backup_data)
                session.modified = True
                logger.info(f"Session restored from backup for {email}: {backup_data}")
            return session
        except Exception as e:
            logger.error(f"Failed to restore session from backup for {email}: {e}")
            return session

    def is_null_session(self, session):
        return not isinstance(session, SecureCookieSession) or not session

    def get_cookie_name(self, app):
        return app.config.get('SESSION_COOKIE_NAME', 'session')

    def get_cookie_domain(self, app):
        return app.config.get('SESSION_COOKIE_DOMAIN', None)

    def get_cookie_path(self, app):
        return app.config.get('SESSION_COOKIE_PATH', '/')

    def get_cookie_httponly(self, app):
        return app.config.get('SESSION_COOKIE_HTTPONLY', True)

    def get_cookie_secure(self, app):
        return app.config.get('SESSION_COOKIE_SECURE', True)

    def get_cookie_samesite(self, app):
        return app.config.get('SESSION_COOKIE_SAMESITE', 'Lax')

    def should_set_cookie(self, app, session):
        return session.modified or app.config.get('SESSION_REFRESH_EACH_REQUEST', True)

app.session_interface = CompressedSession()

# Configure caching
app.config['CACHE_TYPE'] = 'filesystem'
app.config['CACHE_DIR'] = os.path.join(app.root_path, 'cache')
app.config['CACHE_DEFAULT_TIMEOUT'] = 3600  # 1 hour
os.makedirs(app.config['CACHE_DIR'], exist_ok=True)
cache = Cache(app)

# Add a custom validator function 
def non_negative(form, field):
    if field.data < 0:
        raise ValidationError('Value must be non-negative.')

# Custom Jinja2 filter for currency formatting
def format_currency(value, currency='NGN'):
    try:
        formatted = f"{float(value):,.2f}"
        return f"₦{formatted}" if currency == 'NGN' else f"{currency} {formatted}"
    except (ValueError, TypeError):
        logger.error(f"Invalid value for format_currency: {value}")
        return str(value)
app.jinja_env.filters['format_currency'] = format_currency

# Google Sheets setup
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
sheets = None
sheets_lock = threading.Lock()

# Define URL constants
FEEDBACK_FORM_URL = os.getenv('FEEDBACK_FORM_URL', 'https://docs.google.com/forms/d/e/1FAIpQLSfyJIHmGpAvUQV71UnryeWmdGqOMFQC6UMltZ7dC2ZlofVNHg/viewform?usp=header')
WAITLIST_FORM_URL = os.getenv('WAITLIST_FORM_URL', 'https://docs.google.com/forms/d/e/1FAIpQLSeebEf9BZEEzia1EP0S3Q8Wixw_Gqh8qhNhsxCpHIUW8YfyWg/viewform?usp=header')
CONSULTANCY_FORM_URL = os.getenv('CONSULTANCY_FORM_URL', 'https://docs.google.com/forms/d/e/1FAIpQLScsa7rhfhz73dmwbxCFz2E3sBecxExjRxBE8X6e9TxbUgPjZg/viewform?usp=header')
COURSE_URL = os.getenv('COURSE_URL', 'https://example.com/course')
COURSE_TITLE = os.getenv('COURSE_TITLE', 'Learn Budgeting')
LINKEDIN_URL = os.getenv('LINKEDIN_URL', 'https://www.linkedin.com/in/ficore-africa-58913a363/')
TWITTER_URL = os.getenv('TWITTER_URL', 'https://x.com/Ficore_Africa?t=H6sVdRgUYf2GHHG8X5hKbg&s=09')
INVESTING_COURSE_URL = 'https://youtube.com/@ficore.africa?si=myoEpotNALfGK4eI'
SAVINGS_COURSE_URL = 'https://www.youtube.com/@FICORE.AFRICA'
DEBT_COURSE_URL = 'https://www.youtube.com/@FICORE.AFRICA'
RECOVERY_COURSE_URL = 'https://www.youtube.com/@FICORE.AFRICA'

# Define headers
PREDETERMINED_HEADERS_BUDGET = [
    'Timestamp', 'first_name', 'email', 'language', 'monthly_income',
    'housing_expenses', 'food_expenses', 'transport_expenses', 'other_expenses',
    'savings_goal', 'auto_email', 'total_expenses', 'savings', 'surplus_deficit',
    'badges', 'rank', 'total_users'
]
PREDETERMINED_HEADERS_HEALTH = [
    'Timestamp', 'business_name', 'income_revenue', 'expenses_costs', 'debt_loan',
    'debt_interest_rate', 'auto_email', 'phone_number', 'first_name', 'last_name',
    'user_type', 'email', 'badges', 'language'
]
PREDETERMINED_HEADERS_QUIZ = [
    'Timestamp', 'first_name', 'email', 'language', 'question_1', 'answer_1',
    'question_2', 'answer_2', 'question_3', 'answer_3', 'question_4', 'answer_4',
    'question_5', 'answer_5', 'personality', 'badges', 'auto_email'
]

# Translations (extended with quiz-related strings)
translations = {
    'en': {
        # Existing translations (unchanged)
        'First Budget Completed!': 'First Budget Completed!',
        'Check Inbox': 'Check your inbox for the budget report.',
        'Submission Success': 'Budget submitted successfully!',
        'Session Expired': 'Session expired. Please start over.',
        'Incomplete Data': 'Incomplete data. Please complete all steps.',
        'Error retrieving data. Please try again.': 'Error retrieving data. Please try again.',
        'Error saving data. Please try again.': 'Error saving data. Please try again.',
        'Send Email Report': 'Email report sent successfully!',
        'Google Sheets Error': 'Unable to access Google Sheets. Please try again later.',
        'Budget Dashboard': 'Budget Dashboard',
        'Financial growth passport for Africa': 'Financial growth passport for Africa',
        'Welcome': 'Welcome',
        'Your Budget Summary': 'Your Budget Summary',
        'Refresh': 'Refresh',
        '500 Error': 'Server Error',
        'Home': 'Home',
        'Monthly Income': 'Monthly Income',
        'Housing': 'Housing',
        'Food': 'Food',
        'Transport': 'Transport',
        'Other': 'Other',
        'Total Expenses': 'Total Expenses',
        'Savings': 'Savings',
        'Saving': 'Saving',
        'Overspend': 'Overspend',
        'Surplus/Deficit': 'Surplus/Deficit',
        'Advice': 'Advice',
        'Great job! Save or invest your surplus to grow your wealth.': 'Great job! Save or invest your surplus to grow your wealth.',
        'Housing costs are high. Look for cheaper rent or utilities.': 'Housing costs are high. Look for cheaper rent or utilities.',
        'Food spending is high. Try cooking at home more.': 'Food spending is high. Try cooking at home more.',
        'Reduce non-essential spending to balance your budget.': 'Reduce non-essential spending to balance your budget.',
        'Other spending is high. Cut back on non-essentials like clothes or entertainment.': 'Other spending is high. Cut back on non-essentials like clothes or entertainment.',
        'Your ranking': 'Your ranking',
        'Rank': 'Rank',
        'out of': 'out of',
        'users': 'users',
        'Budget Breakdown': 'Budget Breakdown',
        'Income vs Expenses': 'Income vs Expenses',
        'Your Badges': 'Your Badges',
        'Earned badges': 'Earned badges',
        'No Badges Yet': 'No Badges Yet',
        'Quick Tips': 'Quick Tips',
        'Great job! Save or invest your surplus.': 'Great job! Save or invest your surplus.',
        'Keep tracking your expenses every month.': 'Keep tracking your expenses every month.',
        'Spend less on non-essentials to balance your budget.': 'Spend less on non-essentials to balance your budget.',
        'Look for ways to earn extra income.': 'Look for ways to earn extra income.',
        'Recommended Learning': 'Recommended Learning',
        'Learn more about budgeting!': 'Learn more about budgeting!',
        'Whats Next': 'What\'s Next',
        'Back to Home': 'Back to Home',
        'Provide Feedback': 'Provide Feedback',
        'Join Waitlist': 'Join Waitlist',
        'Book Consultancy': 'Book Consultancy',
        'Connect on LinkedIn': 'Connect on LinkedIn',
        'Follow on Twitter': 'Follow on Twitter',
        'Share Your Results': 'Share Your Results',
        'Contact Us': 'Contact Us',
        'Click to Email': 'Click to Email',
        'for support': 'for support',
        'My Budget': 'My Budget',
        'Check yours at': 'Check yours at',
        'Results copied to clipboard': 'Results copied to clipboard',
        'Failed to copy results': 'Failed to copy results',
        'Monthly Budget Planner': 'Monthly Budget Planner',
        'Personal Information': 'Personal Information',
        'First Name': 'First Name',
        'Enter your first name': 'Enter your first name',
        'Enter your first name for your report.': 'Enter your first name for your report.',
        'Email': 'Email',
        'Enter your email': 'Enter your email',
        'Get your budget report by email.': 'Get your budget report by email.',
        'Language': 'Language',
        'Choose your language.': 'Choose your language.',
        'Looks good!': 'Looks good!',
        'First Name Required': 'First Name Required',
        'Invalid Email': 'Invalid Email',
        'Language selected!': 'Language selected!',
        'Language required': 'Language required',
        'Next': 'Next',
        'Continue to Income': 'Continue to Income',
        'Step 1': 'Step 1',
        'Income': 'Income',
        'e.g. ₦150,000': 'e.g. ₦150,000',
        'Your monthly pay or income.': 'Your monthly pay or income.',
        'Valid amount!': 'Valid amount!',
        'Invalid Number': 'Invalid Number',
        'Back': 'Back',
        'Step 2': 'Step 2',
        'Continue to Expenses': 'Continue to Expenses',
        'Please enter a valid income amount': 'Please enter a valid income amount',
        'Expenses': 'Expenses',
        'Housing Expenses': 'Housing Expenses',
        'e.g. ₦30,000': 'e.g. ₦30,000',
        'Rent, electricity, or water bills.': 'Rent, electricity, or water bills.',
        'Food Expenses': 'Food Expenses',
        'e.g. ₦45,000': 'e.g. ₦45,000',
        'Money spent on food each month.': 'Money spent on food each month.',
        'Transport Expenses': 'Transport Expenses',
        'e.g. ₦10,000': 'e.g. ₦10,000',
        'Bus, bike, taxi, or fuel costs.': 'Bus, bike, taxi, or fuel costs.',
        'Other Expenses': 'Other Expenses',
        'e.g. ₦20,000': 'e.g. ₦20,000',
        'Internet, clothes, or other spending.': 'Internet, clothes, or other spending.',
        'Step 3': 'Step 3',
        'Continue to Savings & Review': 'Continue to Savings & Review',
        'Please enter valid amounts for all expenses': 'Please enter valid amounts for all expenses',
        'Savings & Review': 'Savings & Review',
        'Savings Goal': 'Savings Goal',
        'e.g. ₦5,000': 'e.g. ₦5,000',
        'Desired monthly savings amount.': 'Desired monthly savings amount.',
        'Auto Email': 'Auto Email',
        'Submit': 'Submit',
        'Step 4': 'Step 4',
        'Continue to Dashboard': 'Continue to Dashboard',
        'Analyzing your budget': 'Analyzing your budget...',
        'Please enter a valid savings goal amount': 'Please enter a valid savings goal amount',
        'Summary with Emoji': 'Summary 📊',
        'Badges with Emoji': 'Badges 🏅',
        'Tips with Emoji': 'Tips 💡',
        'Budget Report Subject': 'Your Budget Report',
        'Your Budget Report': 'Your Budget Report',
        'Dear': 'Dear',
        'Here is your monthly budget summary.': 'Here is your monthly budget summary.',
        'Budget Summary': 'Budget Summary',
        'Thank you for choosing Ficore Africa!': 'Thank you for choosing Ficore Africa!',
        'Advice with Emoji': 'Advice 💡',
        'Recommended Learning with Emoji': 'Recommended Learning 📚',
        'Your Financial Health Summary': 'Your Financial Health Summary',
        'Your Financial Health Score': 'Your Financial Health Score',
        'Ranked': 'Ranked',
        'Strong Financial Health': 'Your score indicates strong financial health. Focus on investing the surplus funds to grow your wealth.',
        'Stable Finances': 'Your finances are stable but could improve. Consider saving more or reducing your expenses.',
        'Financial Strain': 'Your score suggests financial strain. Prioritize paying off debt and managing your expenses.',
        'Urgent Attention Needed': 'Your finances need urgent attention. Seek professional advice and explore recovery strategies.',
        'Score Breakdown': 'Score Breakdown',
        'Chart Unavailable': 'Chart unavailable at this time.',
        'Score Composition': 'Your score is composed of three components',
        'Cash Flow': 'Cash Flow',
        'Cash Flow Description': 'Reflects how much income remains after expenses. Higher values indicate better financial flexibility.',
        'Debt-to-Income Ratio': 'Debt-to-Income Ratio',
        'Debt-to-Income Description': 'Measures debt relative to income. Lower ratios suggest manageable debt levels.',
        'Debt Interest Burden': 'Debt Interest Burden',
        'Debt Interest Description': 'Indicates the impact of interest rates on your finances. Lower burdens mean less strain from debt.',
        'Balanced Components': 'Your components show balanced financial health. Maintain strong cash flow and low debt.',
        'Components Need Attention': 'One or more components may need attention. Focus on improving cash flow or reducing debt.',
        'Components Indicate Challenges': 'Your components indicate challenges. Work on increasing income, cutting expenses, or lowering debt interest.',
        'Recommended Course': 'Recommended Course',
        'Enroll in': 'Enroll in',
        'Enroll Now': 'Enroll Now',
        'Quick Financial Tips': 'Quick Financial Tips',
        'Invest Wisely': 'Allocate surplus cash to low-risk investments like treasury bills or treasury bonds to grow your wealth.',
        'Scale Smart': 'Reinvest profits into your business to expand operations sustainably.',
        'Build Savings': 'Save 10% of your income monthly to create an emergency fund.',
        'Cut Costs': 'Review needs and wants - check expenses and reduce non-essential spending to boost cash flow.',
        'Reduce Debt': 'Prioritize paying off high-interest loans to ease financial strain.',
        'Boost Income': 'Explore side hustles or new income streams to improve cash flow.',
        'How You Compare': 'How You Compare to Others',
        'Your Rank': 'Your rank of',
        'places you': 'places you',
        'Top 10%': 'in the top 10% of users, indicating exceptional financial health compared to peers.',
        'Top 30%': 'in the top 30%, showing above-average financial stability.',
        'Middle Range': 'in the middle range, suggesting room for improvement to climb the ranks.',
        'Lower Range': 'in the lower range, highlighting the need for strategic financial planning.',
        'Regular Submissions': 'Regular submissions can help track your progress and improve your standing.',
        'Whats Next': 'What’s Next? Unlock Further Insights',
        'Ficore Africa Financial Health Score': 'Ficore Africa Financial Health Score',
        'Get Your Score': 'Get your financial health score and personalized insights instantly!',
        'Enter your first name': 'Enter your first name',
        'First Name Required': 'First name is required.',
        'Enter your last name (optional)': 'Enter your last name (optional)',
        'Confirm your email': 'Confirm your email',
        'Enter phone number (optional)': 'Enter phone number (optional)',
        'User Information': 'User Information',
        'Enter your business name': 'Enter your business name',
        'Business Name Required': 'Business name is required.',
        'User Type': 'User Type',
        'Financial Information': 'Financial Information',
        'Enter monthly income/revenue': 'Enter monthly income/revenue',
        'Enter monthly expenses/costs': 'Enter monthly expenses/costs',
        'Enter total debt/loan amount': 'Enter total debt/loan amount',
        'Enter debt interest rate (%)': 'Enter debt interest rate (%)',
        'Session data missing. Please submit again.': 'Session data is missing. Please submit the form again.',
        'An unexpected error occurred. Please try again.': 'An unexpected error occurred. Please try again.',
        'Error generating plots. Dashboard will display without plots.': 'Error generating plots. The dashboard will display without them.',
        'Top 10% Subject': '🔥 You\'re Top 10%! Your Ficore Score Report Awaits!',
        'Score Report Subject': '📊 Your Ficore Score Report is Ready, {user_name}!',
        'First Health Score Completed!': 'First Health Score Completed!',
        'Financial Stability Achieved!': 'Financial Stability Achieved!',
        'Debt Slayer!': 'Debt Slayer!',
        'Your Financial Health Dashboard': 'Your Financial Health Dashboard',
        'Choose a Tool': 'Choose a Tool',
        'Select an option': 'Select an option',
        'Start': 'Start',
        'Worksheet Not Found': 'The requested worksheet was not found. It has been created automatically.',
        'Invalid Endpoint': 'The requested endpoint is not available. Please try again.',
        # New quiz-related translations
        'Financial Personality Quiz': 'Financial Personality Quiz',
        'Discover Your Financial Personality': 'Discover Your Financial Personality',
        'Start Quiz': 'Start Quiz',
        'Question': 'Question',
        'of': 'of',
        'Submit Quiz': 'Submit Quiz',
        'Personality Unlocked!': 'Personality Unlocked!',
        'Your Financial Personality': 'Your Financial Personality',
        'Habits': 'Habits',
        'Personalized Tip': 'Personalized Tip',
        'Quiz Summary': 'Quiz Summary',
        'Retake Quiz': 'Retake Quiz',
        'Explore Budget Planner': 'Explore Budget Planner',
        'Explore Financial Health Tool': 'Explore Financial Health Tool',
        'Share on WhatsApp': 'Share on WhatsApp',
        'Share on Twitter': 'Share on Twitter',
        'Learn How to Be a Planner': 'Learn How to Be a Planner',
        'Planner': 'Planner: You are organized and track expenses diligently.',
        'Spender': 'Spender: You enjoy spending impulsively, often on entertainment.',
        'Minimalist': 'Minimalist: You live frugally, avoiding unnecessary expenses.',
        'Saver': 'Saver: You prioritize saving and are thrifty with spending.',
        'Avoider': 'Avoider: You tend to ignore budgeting and financial planning.',
        'Planner Tip': 'Keep up your organization! Join a local investment group to grow your savings.',
        'Spender Tip': 'Try setting a weekly entertainment budget to control impulse spending.',
        'Minimalist Tip': 'Your frugality is great! Consider investing small amounts in low-risk options.',
        'Saver Tip': 'Excellent saving habits! Explore treasury bills to make your savings work harder.',
        'Avoider Tip': 'Start small by joining a local savings group to build budgeting habits.',
        'Quiz Report Subject': 'Your Financial Personality Quiz Results'
    },
    'ha': {
        # Existing translations (unchanged)
        'First Budget Completed!': 'An kammala kasafin kuɗi na farko!',
        'Check Inbox': 'Duba akwatin saƙonku don rahoton kasafin kuɗi.',
        'Submission Success': 'An ƙaddamar da kasafin kuɗi cikin nasara!',
        'Session Expired': 'Zaman ya ƙare. Da fatan za a sake farawa.',
        'Incomplete Data': 'Bayanai ba su cika ba. Da fatan za a cika dukkan matakai.',
        'Error retrieving data. Please try again.': 'Kuskure wajen dawo da bayanai. Da fatan za a sake gwadawa.',
        'Error saving data. Please try again.': 'Kuskure wajen ajiye bayanai. Da fatan za a sake gwadawa.',
        'Send Email Report': 'An aika rahoton imel cikin nasara!',
        'Google Sheets Error': 'Ba a iya samun damar Google Sheets ba. Da fatan za a sake gwadawa daga baya.',
        'Budget Dashboard': 'Dashboard na Kasafin Kuɗi',
        'Financial growth passport for Africa': 'Fasfo na ci gaban kuɗi don Afirka',
        'Welcome': 'Barka da Zuwa',
        'Your Budget Summary': 'Takaitaccen Kasafin Kuɗin Ku',
        'Refresh': 'Sabunta',
        'Monthly Income': 'Kuɗin Shiga na Wata',
        'Housing': 'Gida',
        'Food': 'Abinci',
        'Transport': 'Sufuri',
        'Other': 'Sauran',
        'Total Expenses': 'Jimlar Kuɗaɗe',
        'Savings': 'Tattara Kuɗi',
        'Saving': 'Tara Kuɗi',
        'Overspend': 'Kashe kudi yayi yawa',
        'Surplus/Deficit': 'Rage/Riba',
        'Advice': 'Shawara',
        'Great job! Save or invest your surplus to grow your wealth.': 'Aiki mai kyau! Ajiye ko saka ragowar kuɗin ku don bunkasa arzikinku.',
        'Housing costs are high. Look for cheaper rent or utilities.': 'Kuɗin gida yana da yawa. Nemi haya mai rahusa ko kayan aiki.',
        'Food spending is high. Try cooking at home more.': 'Kuɗin abinci yana da yawa. Gwada dafa abinci a gida sosai.',
        'Reduce non-essential spending to balance your budget.': 'Rage kashe kuɗi marasa mahimmanci don daidaita kasafin kuɗin ku.',
        'Other spending is high. Cut back on non-essentials like clothes or entertainment.': 'Sauran kashe kuɗi yana da yawa. Rage abubuwan da ba su da mahimmanci kamar tufafi ko nishaɗi.',
        'Your ranking': 'Matsayin ku',
        'Rank': 'Matsayi',
        'out of': 'daga cikin',
        'users': 'masu amfani',
        'Budget Breakdown': 'Rarraba Kasafin Kuɗi',
        'Income vs Expenses': 'Kuɗin Shiga vs Kuɗaɗe',
        'Your Badges': 'Alamominku',
        'Earned badges': 'Alamomīn da aka samu',
        'No Badges Yet': 'Babu Alama Har Yanzu',
        'Quick Tips': 'Shawarwari masu Sauƙi',
        'Great job! Save or invest your surplus.': 'Aiki mai kyau! Ajiye ko saka ragowar kuɗin ku.',
        'Keep tracking your expenses every month.': 'Ci gaba da bin diddigin kuɗaɗen ku kowane wata.',
        'Spend less on non-essentials to balance your budget.': 'Kashe ƙasa da kima akan abubuwan da ba su da mahimmanci don daidaita kasafin kuɗin ku.',
        'Look for ways to earn extra income.': 'Nemi hanyoyin samun ƙarin kuɗin shiga.',
        'Recommended Learning': 'Koyon da Aka Shawarta',
        'Learn more about budgeting!': 'Ƙara koyo game da tsara kasafin kuɗi!',
        'Whats Next': 'Me ke Gaba',
        'Back to Home': 'Koma Gida',
        'Home': 'Shafin Farko',
        'Provide Feedback': 'Bayar da Shawara',
        'Join Waitlist': 'Shiga Jerin Jira',
        'Book Consultancy': 'Yi Alƙawarin Shawara',
        'Connect on LinkedIn': 'Haɗa a LinkedIn',
        'Follow on Twitter': 'Bi a Twitter',
        'Share Your Results': 'Raba Sakamakonku',
        'Contact Us': 'Tuntuɓe Mu',
        'Click to Email': 'Danna don Imel',
        'for support': 'don tallafi',
        'My Budget': 'Kasafin Kuɗina',
        'Check yours at': 'Duba naku a',
        'Results copied to clipboard': 'An kwafi sakamakon zuwa allo',
        'Failed to copy results': 'An kasa kwafi sakamakon',
        'Monthly Budget Planner': 'Mai Tsara Kasafin Kuɗi na Wata',
        'Personal Information': 'Bayanai na Kai',
        'First Name': 'Sunan Farko',
        'Enter your first name': 'Shigar da sunan farko',
        'Enter your first name for your report.': 'Shigar da sunan farko don rahotonku.',
        'Email': 'Imel',
        'Enter your email': 'Shigar da imel ɗin ku',
        'Get your budget report by email.': 'Samu rahoton kasafin kuɗin ku ta imel.',
        'Language': 'Yare',
        'Choose your language.': 'Zaɓi yarenku.',
        'Looks good!': 'Yana da kyau!',
        'First Name Required': 'Ana Buƙatar Sunan Farko',
        'Invalid Email': 'Imel Ba daidai ba ne',
        'Language selected!': 'An zaɓi yare!',
        'Language required': 'Ana buƙatar yare',
        'Next': 'Na Gaba',
        'Continue to Income': 'Ci gaba zuwa Kuɗin Shiga',
        'Step 1': 'Mataki na 1',
        'Income': 'Kuɗin Shiga',
        'e.g. ₦150,000': 'misali ₦150,000',
        'Your monthly pay or income.': 'Albashin ku na wata ko kuɗin shiga.',
        'Valid amount!': 'Adadin daidai ne!',
        'Invalid Number': 'Lamba Ba daidai ba ne',
        'Back': 'Koma Baya',
        'Step 2': 'Mataki na 2',
        'Continue to Expenses': 'Ci gaba zuwa Kashe Kuɗi',
        'Please enter a valid income amount': 'Da fatan za a shigar da adadin kuɗin shiga mai inganci',
        'Expenses': 'Kuɗaɗe',
        'Housing Expenses': 'Kuɗin Gida',
        'e.g. ₦30,000': 'misali ₦30,000',
        'Rent, electricity, or water bills.': 'Haya, wutar lantarki, ko kuɗin ruwa.',
        'Food Expenses': 'Kuɗin Abinci',
        'e.g. ₦45,000': 'misali ₦45,000',
        'Money spent on food each month.': 'Kuɗin da aka kashe akan abinci kowane wata.',
        'Transport Expenses': 'Kuɗin Sufuri',
        'e.g. ₦10,000': 'misali ₦10,000',
        'Bus, bike, taxi, or fuel costs.': 'Bas, keke, taksi, ko kuɗin mai.',
        'Other Expenses': 'Sauran Kuɗaɗe',
        'e.g. ₦20,000': 'misali ₦20,000',
        'Internet, clothes, or other spending.': 'Intanet, tufafi, ko sauran kashe kuɗi.',
        'Step 3': 'Mataki na 3',
        'Continue to Savings & Review': 'Ci gaba zuwa Tattalin Arziki & Dubawa',
        'Please enter valid amounts for all expenses': 'Da fatan za a shigar da adadin da ya dace ga duk kashe kuɗi',
        'Savings & Review': 'Tattara Kuɗi & Dubawa',
        'Savings Goal': 'Manufar Tattara Kuɗi',
        'e.g. ₦5,000': 'misali ₦5,000',
        'Desired monthly savings amount.': 'Adadin tattara kuɗi na wata da ake so.',
        'Auto Email': 'Imel ta atomatik',
        'Submit': 'Sallama',
        'Step 4': 'Mataki na 4',
        'Continue to Dashboard': 'Ci gaba zuwa Dashboard',
        'Analyzing your budget': 'Nazarin kasafin kuɗin ku...',
        'Please enter a valid savings goal amount': 'Da fatan za a shigar da adadin manufa mai inganci',
        'Summary with Emoji': 'Taƙaice 📊',
        'Badges with Emoji': 'Baja 🏅',
        'Tips with Emoji': 'Shawara 💡',
        'Budget Report Subject': 'Rahoton Kasafin Kuɗi',
        'Your Budget Report': 'Rahoton Kasafin Kuɗi',
        'Dear': 'Masoyi',
        'Here is your monthly budget summary.': 'Ga takaitaccen kasafin kuɗin ku na wata.',
        'Budget Summary': 'Takaitaccen Kasafin Kuɗi',
        'Thank you for choosing Ficore Africa!': 'Muna godiya da zaɓin Ficore Afirka!',
        'Advice with Emoji': 'Shawara 💡',
        'Recommended Learning with Emoji': 'Koyon da Aka Shawarta 📚',
        'Your Financial Health Summary': 'Takaitaccen Bayanai Akan Lafiyar Kuɗin Ku!',
        'Your Financial Health Score': 'Maki Da Lafiyar Kuɗin Ku Ta Samu:',
        'Ranked': 'Darajar Lafiyar Kuɗin Ku',
        'out of': 'Daga Cikin',
        'users': 'Dukkan Masu Amfani Da Ficore Zuwa Yanzu.',
        'Strong Financial Health': 'Makin ku yana nuna ƙarfin lafiyar kuɗinku. Ku Mai da hankali kan zuba hannun jari daga cikin kuɗin da ya rage muku don haɓaka dukiyarku.',
        'Stable Finances': 'Makin Kuɗin ku suna Nuni da kwanciyar hankali, amma zaku iya ingantashi duk da haka. Yi la’akari da adanawa ko rage wani bangare na kuɗin ta hanyar ajiya don gaba.',
        'Financial Strain': 'Makin ku yana nuna Akwai damuwar kuɗi. Ku Fifita biyan bashi sannan ku sarrafa kashe kuɗinku dakyau.',
        'Urgent Attention Needed': 'Makin Kuɗin ku suna Nuna buƙatar kulawa cikin gaggawa. Ku Nemi shawarar ƙwararru kuma Ku bincika dabarun farfadowa daga wannan yanayi.',
        'Score Breakdown': 'Rarraban Makin ku',
        'Chart Unavailable': 'Zanen Lissafi ba ya samuwa a wannan lokacin saboda Netowrk.',
        'Score Composition': 'Makin ku ya ƙunshi abubuporter wa uku',
        'Cash Flow': 'Kuɗin da Kuke Samu',
        'Cash Flow Description': 'Yana nuna adadin kuɗin da ya rage muku a hannu bayan Kun kashe kuɗi wajen biyan Bukatu. Maki mai ƙima yana nuna mafi kyawun alamar rike kuɗi.',
        'Debt-to-Income Ratio': 'Rabiyar Bashi zuwa Kuɗin shiga',
        'Debt-to-Income Description': 'Yana auna bashi dangane da kuɗin shiga. Ƙananan Makin rabiya yana nuna matakan bashi mai sauƙi.',
        'Debt Interest Burden': 'Nauyin Interest akan Bashi',
        'Debt Interest Description': 'Yana nuna tasirin ƙimar Interest a kan kuɗin ku. Ƙananan nauyi yana nufin ƙarancin damuwa daga Interest akan bashi.',
        'Balanced Components': 'Abubuwan da ke ciki suna nuna daidaitaccen lafiyar kuɗi. Ci gaba da kiyaye kuɗi ta hanya mai kyau kuma da ƙarancin bashi.',
        'Components Need Attention': 'Ɗaya ko fiye da abubuwan da ke ciki na iya buƙatar kulawa. Mai da hankali kan inganta samun kuɗi ko rage bashi.',
        'Components Indicate Challenges': 'Abubuwan da ke ciki suna nuna ƙalubale. Yi aiki kan ورهara kuɗin shiga, rage kashe kuɗin, ko rage Interest da kake biya akan bashi.',
        'Your Badges': 'Lambar Yabo',
        'No Badges Yet': 'Ba a sami Lambar Yabo ba tukuna. Ci gaba da Aiki da Ficore don samun Sabbin Lambobin Yabo!',
        'Recommended Learning': 'Shawari aka Koyon Inganta Neman Kudi da Ajiya.',
        'Recommended Course': 'Darasi da Aka Shawarta Maka',
        'Enroll in': 'Shiga ciki',
        'Enroll Now': 'Shiga Yanzu',
        'Quick Financial Tips': 'Shawarwari',
        'Invest Wisely': 'Sanya kuɗin da ya rage maka a cikin hannayen jari masu ƙarancin haɗari kamar takardun shaida daga Gwamnati ko Manyan Kamfanuwa don haɓaka dukiyarku.',
        'Scale Smart': 'Sake saka ribar kasuwancinku a cikin kasuwancin naku don faɗaɗa shi domin dorewa.',
        'Build Savings': 'Ajiye 10% na kuɗin shigarka a kowane wata don Samar da Asusun gaggawa domin rashin Lafiya ko jarabawa.',
        'Cut Costs': 'Kula da kashe kuɗinku kuma ku rage kashe kuɗin da ba dole ba don haɓaka arzikinku.',
        'Reduce Debt': 'Fifita biyan Bashi masu Interest don sauƙaƙe damuwar kuɗi.',
        'Boost Income': 'Bincika ayyukan a gefe ko ka nemi sabbin hanyoyin samun kuɗin don inganta Arzikinka.',
        'How You Compare': 'Kwatanta ku da Sauran Masu Amfani da Ficore',
        'Your Rank': 'Matsayin ku',
        'places you': 'ya sanya ku',
        'Top 10%': 'a cikin sama da kaso goma 10% na masu amfani da Ficore, yana nuna akwai kyawun lafiyar kuɗi idan aka kwatanta da Sauran Mutane.',
        'Top 30%': 'a cikin sama da kaso talatin 30%, yana nuna akwai kwanciyar hankali na kuɗi sama da yawancin Mutane.',
        'Middle Range': 'a cikin tsaka-tsaki, yana nuna akwai sarari don inganta samu domin hawa matsayi na gaba.',
        'Lower Range': 'a cikin mataki na ƙasa, yana nuna akwai buƙatar ku tsara kuɗinku dakyau cikin dabara daga yanzu.',
        'Regular Submissions': 'Amfani da Ficore akai-akai zai taimaka muku wajen bin diddigin ci gaban ku da kanku, don inganta matsayin Arzikinku.',
        'Whats Next': 'Me ke Gaba? Ku Duba Wadannan:',
        'Back to Home': 'Koma Sahfin Farko',
        'Provide Feedback': 'Danna Idan Kana da Shawara',
        'Join Waitlist': 'Masu Jiran Ficore Premium',
        'Book Consultancy': 'Jerin Masu Neman Shawara',
        'Contact Us': 'Tuntube Mu a',
        'for support': 'Don Tura Sako',
        'Click to Email': 'Danna Don Tura Sako',
        'Ficore Africa Financial Health Score': 'Makin Lafiyar KuɗinKu Daga Ficore Africa',
        'Get Your Score': 'Sami makin lafiyar kuɗin ku don fahimtar keɓaɓɓun hanyoyin Ingantawa nan take!',
        'Personal Information': 'Bayanan Kai',
        'Enter your first name': 'Shigar da sunanka na farko',
        'First Name Required': 'Ana buƙatar sunan farko.',
        'Enter your last name (optional)': 'Shigar da sunanka na ƙarshe (na zaɓi)',
        'Enter your email': 'Shigar da email ɗinka',
        'Invalid Email': 'Da fatan za a shigar da adireshin email mai inganci.',
        'Confirm your email': 'Sake Tabbatar da email ɗinka',
        'Enter phone number (optional)': 'Shigar da lambar waya (na zaɓi)',
        'User Information': 'Bayanan Ka',
        'Enter your business name': 'Shigar da sunan kasuwancinka',
        'Business Name Required': 'Ana buƙatar sunan kasuwanci.',
        'User Type': 'Nau’in Mai Amfani da Ficore',
        'Financial Information': 'Bayanan Kuɗi',
        'Enter monthly income/revenue': 'Shigar da jimillar kuɗin shiga/kudin shigarku na wata-wata',
        'Enter monthly expenses/costs': 'Shigar da jimillar kashe kuɗinku/kudin wata-wata',
        'Enter total debt/loan amount': 'Shigar da jimillar bashi/lamuni',
        'Enter debt interest rate (%)': 'Shigar da Interest na bashin (%)',
        'Invalid Number': 'A shigar da lamba mai daidai.',
        'Error saving data. Please try again.': 'Anyi Kuskure wajen adana bayanai. Da fatan za a sake gwadawa.',
        'Error retrieving data. Please try again.': 'Anyi Kuskure wajen dawo da bayanai. Da fatan za a sake gwadawa.',
        'Error retrieving user data. Please try again.': 'Anyi Kuskure wajen dawo da bayanai masu amfani. Da fatan za a sake gwadawa.',
        'An unexpected error occurred. Please try again.': 'Wani kuskure wanda ba a zata ba ya faru. Da fatan za a sake gwadawa.',
        'Session Expired': 'Lokacin aikin ku ya ƙare. Da fatan za a sake shigar da shafin kuma a gwada sake.',
        'Top 10% Subject': '🔥 Kuna cikin Sama da kaso goma 10%! Rahoton Makin ku na Ficore Yana Jiran Ku!',
        'Score Report Subject': '📊 Rahoton Makin ku na Ficore Yana Shirye, {user_name}!',
        'First Health Score Completed!': 'Makin Lafiyar Arziki na Farko ya Kammala!',
        'Financial Stability Achieved!': 'Akwai Wadata!',
        'Debt Slayer!': 'Mai Ragargaza Bashi!',
        'Session data missing. Please submit again.': 'Bayanan zama sun ɓace. Da fatan za a sake yin aikace.',
        'An unexpected error occurred. Please try again.': 'Wani kuskure wanda ba a zata ba ya faru. Da fatan za a sake gwadawa.',
        'Error generating plots. Dashboard will display without plots.': 'An sami kuskure wajen ƙirƙirar zane. Allon zai nuna ba tare da su ba.',
        'Submission Success': 'An shigar da bayananka cikin nasara! Duba allon bayananka a ƙasa 👇',
        'Check Inbox': 'Da fatan za a duba akwatin saƙonku Inbox ko foldar na Spam ko Junk idan email ɗin bai zo ba cikin ƴan mintuna.',
        'Your Financial Health Dashboard': 'Allon Lafiyar Kuɗin Ku',
        'Choose a Tool': 'Zaɓi Kayan Aiki',
        'Select an option': 'Zaɓi wani zaɓi',
        'Start': 'Fara',
        '500 Error': 'Server Error',
        'Worksheet Not Found': 'Lambar da ake nema ba ta samu ba. An ƙirƙira ta ta atomatik.',
        'Invalid Endpoint': 'Wurin da ake nema ba ya samuwa. Da fatan za a sake gwadawa.',
        # New quiz-related translations
        'Financial Personality Quiz': 'Tambayoyin Halin Kuɗi',
        'Discover Your Financial Personality': 'Gano Halin Kuɗin Ku',
        'Start Quiz': 'Fara Tambayoyi',
        'Question': 'Tambaya',
        'of': 'na',
        'Submit Quiz': 'Mika Tambayoyi',
        'Personality Unlocked!': 'An Buɗe Halin Ku!',
        'Your Financial Personality': 'Halin Kuɗin Ku',
        'Habits': 'Al’ada',
        'Personalized Tip': 'Shawara ta Keɓaɓɓu',
        'Quiz Summary': 'Taƙaitaccen Tambayoyi',
        'Retake Quiz': 'Sake Yin Tambayoyi',
        'Explore Budget Planner': 'Bincika Mai Tsara Kasafin Kuɗi',
        'Explore Financial Health Tool': 'Bincika Kayan Lafiyar Kuɗi',
        'Share on WhatsApp': 'Raba akan WhatsApp',
        'Share on Twitter': 'Raba akan Twitter',
        'Learn How to Be a Planner': 'Koyi Yadda Ake Zama Mai Shirya Kuɗi',
        'Planner': 'Mai Shirya Kuɗi: Kuna da tsari kuma kuna bin diddigin kashe kuɗinku da kyau.',
        'Spender': 'Mai Kashe Kuɗi: Kuna ji daɗin kashe kuɗi ba tare da tsari ba, sau da yawa akan nishaɗi.',
        'Minimalist': 'Mai Ragin Kashe Kuɗi: Kuna rayuwa cikin sauƙi, kuna guje wa kashe kuɗi maras muhimmanci.',
        'Saver': 'Mai Tattara Kuɗi: Kuna fifita tattara kuɗi kuma kuna taka tsantsan wajen kashewa.',
        'Avoider': 'Mai Gujewa: Kuna yawan gujewa tsara kasafin kuɗi da shirin kuɗi.',
        'Planner Tip': 'Ci gaba da tsarinku! Shiga ƙungiyar zuba jari ta gida don haɓaka ajiyarku.',
        'Spender Tip': 'Gwada saita kasafin nishaɗi na mako don sarrafa kashe kuɗi ba tare da tsari ba.',
        'Minimalist Tip': 'Raginku mai kyau ne! Yi la’akari da saka ƙananan kuɗi a zabin da ba shi da haɗari.',
        'Saver Tip': 'Kyakkyawan al’adar tattara kuɗi! Bincika takardun shaida don sanya ajiyarku ta yi aiki sosai.',
        'Avoider Tip': 'Fara da ƙanƙanta ta hanyar shiga ƙungiyar ajiya ta gida don gina al’adar kasafin kuɗi.',
        'Quiz Report Subject': 'Sakamakon Tambayoyin Halin Kuɗin Ku'
    }
}

def sanitize_input(text):
    if not text:
        return text
    text = re.sub(r'[<>";]', '', text.strip())[:100]
    return text

def get_sheets_client():
    global sheets
    if sheets is None:
        logger.error("Google Sheets client not initialized.")
        return None
    return sheets

def set_sheet_headers(headers, worksheet_name='Health'):
    try:
        client = get_sheets_client()
        if client is None:
            logger.error(f"Google Sheets client not initialized for setting headers in worksheet '{worksheet_name}'.")
            return False
        try:
            worksheet = client.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            logger.info(f"Worksheet '{worksheet_name}' not found. Creating new worksheet.")
            client.add_worksheet(worksheet_name, rows=100, cols=len(headers))
            worksheet = client.worksheet(worksheet_name)
        worksheet.update('A1:' + chr(64 + len(headers)) + '1', [headers])
        logger.info(f"Headers updated in worksheet '{worksheet_name}'.")
        return True
    except gspread.exceptions.APIError as e:
        logger.error(f"Google Sheets API error setting headers in worksheet '{worksheet_name}': {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error setting headers in worksheet '{worksheet_name}': {e}")
        return False

def initialize_sheets(max_retries=5, backoff_factor=2):
    global sheets
    if not SPREADSHEET_ID or SPREADSHEET_ID == "None":
        logger.critical("SPREADSHEET_ID is not set or invalid.")
        return False
    for attempt in range(max_retries):
        try:
            creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
            if not creds_json:
                logger.critical("GOOGLE_CREDENTIALS_JSON not set.")
                return False
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPE)
            client = gspread.authorize(creds)
            sheets = client.open_by_key(SPREADSHEET_ID)
            # Verify or set headers for worksheets
            set_sheet_headers(PREDETERMINED_HEADERS_HEALTH, 'Health')
            set_sheet_headers(PREDETERMINED_HEADERS_BUDGET, 'Budget')
            set_sheet_headers(PREDETERMINED_HEADERS_QUIZ, 'Quiz')
            logger.info("Successfully initialized Google Sheets.")
            return True
        except json.JSONDecodeError as e:
            logger.error(f"Invalid GOOGLE_CREDENTIALS_JSON format: {e}")
            return False
        except gspread.exceptions.APIError as e:
            logger.error(f"Google Sheets API error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
    logger.critical("Max retries exceeded for Google Sheets initialization.")
    return False

# Initialize Google Sheets at startup
if not initialize_sheets():
    raise RuntimeError("Failed to initialize Google Sheets at startup.")

@cache.memoize(timeout=3600)
def fetch_data_from_sheet(email=None, headers=PREDETERMINED_HEADERS_HEALTH, worksheet_name='Health', max_retries=5, backoff_factor=2):
    for attempt in range(max_retries):
        try:
            client = get_sheets_client()
            if client is None:
                logger.error(f"Attempt {attempt + 1}: Google Sheets client not initialized.")
                return pd.DataFrame(columns=headers)
            try:
                worksheet = client.worksheet(worksheet_name)
            except gspread.exceptions.WorksheetNotFound:
                logger.info(f"Worksheet '{worksheet_name}' not found. Creating new worksheet.")
                client.add_worksheet(worksheet_name, rows=100, cols=len(headers))
                worksheet = client.worksheet(worksheet_name)
                worksheet.update('A1:' + chr(64 + len(headers)) + '1', [headers])
            values = worksheet.get_all_values()
            if not values:
                logger.info(f"Attempt {attempt + 1}: No data in worksheet '{worksheet_name}'.")
                return pd.DataFrame(columns=headers)
            headers_actual = values[0]
            rows = values[1:] if len(values) > 1 else []
            adjusted_rows = [
                row + [''] * (len(headers) - len(row)) if len(row) < len(headers) else row[:len(headers)]
                for row in rows
            ]
            df = pd.DataFrame(adjusted_rows, columns=headers)
            df['language'] = df['language'].replace('', 'en').apply(lambda x: x if x in translations else 'en')
            if email:
                df = df[df['email'] == email].head(1) if headers == PREDETERMINED_HEADERS_BUDGET else df[df['email'] == email]
            logger.info(f"Fetched {len(df)} rows for email {email if email else 'all'} from worksheet '{worksheet_name}'.")
            return df
        except gspread.exceptions.APIError as e:
            logger.error(f"Google Sheets API error on attempt {attempt + 1} for worksheet '{worksheet_name}': {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed for worksheet '{worksheet_name}': {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
    logger.error(f"Max retries reached while fetching data from worksheet '{worksheet_name}'.")
    return pd.DataFrame(columns=headers)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def append_to_sheet(data, headers, worksheet_name='Health', max_retries=3, backoff_factor=2):
    for attempt in range(max_retries):
        try:
            if not data or len(data) != len(headers):
                logger.error(f"Invalid data length ({len(data)}) for headers ({len(headers)}) in worksheet '{worksheet_name}': {data}")
                return False
            client = get_sheets_client()
            if client is None:
                logger.error(f"Google Sheets client not initialized for appending data to worksheet '{worksheet_name}'.")
                return False
            try:
                worksheet = client.worksheet(worksheet_name)
            except gspread.exceptions.WorksheetNotFound:
                logger.info(f"Worksheet '{worksheet_name}' not found. Creating new worksheet.")
                client.add_worksheet(worksheet_name, rows=100, cols=len(headers))
                worksheet = client.worksheet(worksheet_name)
                worksheet.update('A1:' + chr(64 + len(headers)) + '1', [headers])
            current_headers = worksheet.row_values(1)
            if not current_headers or current_headers != headers:
                logger.info(f"Headers missing or incorrect in worksheet '{worksheet_name}'. Setting headers.")
                if not set_sheet_headers(headers, worksheet_name):
                    logger.error(f"Failed to set sheet headers in worksheet '{worksheet_name}'.")
                    return False
            worksheet.append_row(data, value_input_option='RAW')
            logger.info(f"Appended data to worksheet '{worksheet_name}': {data}")
            time.sleep(1)  # Respect API rate limits
            return True
        except gspread.exceptions.APIError as e:
            logger.error(f"Attempt {attempt + 1} Google Sheets API error appending to worksheet '{worksheet_name}': {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} Unexpected error appending to worksheet '{worksheet_name}': {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
    logger.error(f"Max retries reached while appending to worksheet '{worksheet_name}'.")
    return False

def calculate_budget_metrics(df):
    try:
        if df.empty:
            logger.warning("Empty DataFrame in calculate_budget_metrics.")
            return df
        for col in ['monthly_income', 'housing_expenses', 'food_expenses', 'transport_expenses', 'other_expenses', 'savings_goal']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
        df['total_expenses'] = df['housing_expenses'] + df['food_expenses'] + df['transport_expenses'] + df['other_expenses']
        df['savings'] = df.apply(
            lambda row: max(0, row['monthly_income'] * 0.1) if pd.isna(row['savings_goal']) or row['savings_goal'] == 0 else row['savings_goal'],
            axis=1
        )
        df['surplus_deficit'] = df['monthly_income'] - df['total_expenses'] - df['savings']
        df['outcome_status'] = df['surplus_deficit'].apply(
            lambda x: 'Savings' if x >= 0 else 'Overspend'
        )
        df['advice'] = df['surplus_deficit'].apply(
            lambda x: translations[df['language'].iloc[0]]['Great job! Save or invest your surplus to grow your wealth.'] if x >= 0 
            else translations[df['language'].iloc[0]]['Reduce non-essential spending to balance your budget.']
        )
        return df
    except Exception as e:
        logger.error(f"Error in calculate_budget_metrics: {e}")
        return df

def assign_badges_budget(user_df):
    badges = []
    try:
        if user_df.empty:
            logger.warning("Empty user_df in assign_badges_budget.")
            return badges
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]
        language = user_row.get('language', 'en')
        if language not in translations:
            logger.warning(f"Invalid language '{language}'. Defaulting to English.")
            language = 'en'
        if len(user_df) == 1:
            badges.append(translations[language]['First Budget Completed!'])
        return badges
    except Exception as e:
        logger.error(f"Error in assign_badges_budget: {e}")
        return badges

def calculate_health_score(df):
    try:
        if df.empty:
            logger.warning("Empty DataFrame in calculate_health_score.")
            df['HealthScore'] = 0.0
            return df
        required_cols = ['income_revenue', 'expenses_costs', 'debt_loan', 'debt_interest_rate']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            logger.error(f"Missing required columns in DataFrame: {missing_cols}")
            df['HealthScore'] = 0.0
            return df
        for col in required_cols:
            df[col] = df[col].apply(
                lambda x: float(re.sub(r'[,]', '', str(x))) if isinstance(x, str) and x.strip() else (float(x) if pd.notnull(x) else 0.0)
            ).fillna(0.0)
        df['HealthScore'] = 0.0
        df['IncomeRevenueSafe'] = df['income_revenue'].replace(0, 1e-10)
        df['CashFlowRatio'] = (df['income_revenue'] - df['expenses_costs']) / df['IncomeRevenueSafe']
        df['DebtToIncomeRatio'] = df['debt_loan'] / df['IncomeRevenueSafe']
        df['DebtInterestBurden'] = df['debt_interest_rate'].clip(lower=0) / 20
        df['DebtInterestBurden'] = df['DebtInterestBurden'].clip(upper=1)
        df['NormCashFlow'] = df['CashFlowRatio'].clip(0, 1)
        df['NormDebtToIncome'] = 1 - df['DebtToIncomeRatio'].clip(0, 1)
        df['NormDebtInterest'] = 1 - df['DebtInterestBurden']
        df['HealthScore'] = (
            df['NormCashFlow'] * 0.333 +
            df['NormDebtToIncome'] * 0.333 +
            df['NormDebtInterest'] * 0.333
        ) * 100
        df['HealthScore'] = df['HealthScore'].round(2)
        df[['ScoreDescription', 'CourseTitle', 'CourseURL']] = df.apply(
            score_description_and_course, axis=1, result_type='expand'
        )
        logger.debug(f"HealthScore calculated successfully: {df['HealthScore'].iloc[0]}")
        return df
    except Exception as e:
        logger.error(f"Error calculating health score: {e}\n{traceback.format_exc()}")
        df['HealthScore'] = 0.0
        return df

def score_description_and_course(row):
    score = row['HealthScore']
    cash_flow = row['CashFlowRatio']
    debt_to_income = row['DebtToIncomeRatio']
    debt_interest = row['DebtInterestBurden']
    clean_investing_url = INVESTING_COURSE_URL.split('?')[0]
    clean_debt_url = DEBT_COURSE_URL.split('?')[0]
    clean_savings_url = SAVINGS_COURSE_URL.split('?')[0]
    clean_recovery_url = RECOVERY_COURSE_URL.split('?')[0]
    if score >= 75:
        return ('Stable Income; invest excess now', 'Ficore Simplified Investing Course: How to Invest in 2025 for Better Gains', clean_investing_url)
    elif score >= 50:
        if cash_flow < 0.3 or debt_interest > 0.5:
            return ('At Risk; manage your expense!', 'Ficore Debt and Expense Management: Regain Control in 2025', clean_debt_url)
        return ('Moderate; save something monthly!', 'Ficore Savings Mastery: Building a Financial Safety Net in 2025', clean_savings_url)
    elif score >= 25:
        if debt_to_income > 0.5 or debt_interest > 0.5:
            return ('At Risk; pay off debt, manage your expense!', 'Ficore Debt and Expense Management: Regain Control in 2025', clean_debt_url)
        return ('At Risk; manage your expense!', 'Ficore Debt and Expense Management: Regain Control in 2025', clean_debt_url)
    else:
        if debt_to_income > 0.5 or cash_flow < 0.3:
            return ('Critical; add source of income! pay off debt! manage your expense!', 'Ficore Financial Recovery: First Steps to Stability in 2025', clean_recovery_url)
        return ('Critical; seek financial help and advice!', 'Ficore Financial Recovery: First Steps to Stability in 2025', clean_recovery_url)

def assign_badges_health(user_df, all_users_df):
    badges = []
    if user_df.empty:
        logger.warning("Empty user_df in assign_badges_health.")
        return badges
    try:
        if 'Timestamp' not in user_df.columns:
            logger.error("Timestamp column missing in user_df. Cannot assign badges.")
            return badges
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', dayfirst=True, errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]
        email = user_row['email']
        health_score = user_row['HealthScore']
        language = user_row['language']
        if language not in translations:
            logger.warning(f"Invalid language '{language}' for user {email}. Defaulting to English.")
            language = 'en'
        if len(user_df) == 1:
            badges.append(translations[language]['First Health Score Completed!'])
        if health_score >= 50:
            badges.append(translations[language]['Financial Stability Achieved!'])
        if user_row['DebtToIncomeRatio'] < 0.3:
            badges.append(translations[language]['Debt Slayer!'])
        return badges
    except Exception as e:
        logger.error(f"Error in assign_badges_health: {e}")
        return badges

def send_health_email(to_email, user_name, health_score, score_description, rank, total_users, course_title, course_url, language):
    try:
        if language not in translations:
            logger.warning(f"Invalid language '{language}' for email to {to_email}. Defaulting to English.")
            language = 'en'
        subject = translations[language]['Top 10% Subject'] if rank <= total_users * 0.1 else translations[language]['Score Report Subject'].format(user_name=user_name)
        msg = Message(
            subject=subject,
            recipients=[to_email],
            html=render_template(
                'health_score_email.html',
                trans=translations[language],
                user_name=sanitize_input(user_name),
                health_score=health_score,
                score_description=score_description,
                rank=rank,
                total_users=total_users,
                course_title=course_title,
                course_url=course_url,
                FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
                WAITLIST_FORM_URL=WAITLIST_FORM_URL,
                CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
                linkedin_url=LINKEDIN_URL,
                twitter_url=TWITTER_URL,
                language=language
            )
        )
        mail.send(msg)
        logger.info(f"Health email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error sending health email to {to_email}: {e}")
        return False

def send_health_email_async(to_email, user_name, health_score, score_description, rank, total_users, course_title, course_url, language):
    try:
        with app.app_context():
            send_health_email(to_email, user_name, health_score, score_description, rank, total_users, course_title, course_url, language)
    except Exception as e:
        logger.error(f"Async email sending failed for {to_email}: {e}", exc_info=True)

def generate_breakdown_plot(user_df):
    try:
        if user_df.empty:
            return None
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', dayfirst=True, errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_row = user_df.iloc[0]
        labels = ['Cash Flow', 'Debt-to-Income', 'Debt Interest']
        values = [
            user_row['NormCashFlow'] * 100 / 3,
            user_row['NormDebtToIncome'] * 100 / 3,
            user_row['NormDebtInterest'] * 100 / 3
        ]
        fig = px.pie(names=labels, values=values, title='Score Breakdown')
        fig.update_layout(
            margin=dict(l=0, r=0, t=0, b=0),
            height=200,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)'
        )
        return fig.to_html(full_html=False, include_plotlyjs=False)
    except Exception as e:
        logger.error(f"Error generating breakdown plot: {e}", exc_info=True)
        return None

def generate_comparison_plot(user_df, all_users_df):
    try:
        if user_df.empty or all_users_df.empty:
            return None
        user_df['Timestamp'] = pd.to_datetime(user_df['Timestamp'], format='mixed', dayfirst=True, errors='coerce')
        user_df = user_df.sort_values('Timestamp', ascending=False)
        user_score = user_df.iloc[0]['HealthScore']
        scores = all_users_df['HealthScore'].astype(float)
        fig = px.histogram(
            x=scores,
            nbins=20,
            title='How Your Score Compares',
            labels={'x': 'Financial Health Score', 'y': 'Number of Users'}
        )
        fig.add_vline(x=user_score, line_dash="dash", line_color="red")
        fig.update_layout(
            margin=dict(l=0, r=0, t=0, b=0),
            height=200,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)'
        )
        return fig.to_html(full_html=False, include_plotlyjs=False)
    except Exception as e:
        logger.error(f"Error generating comparison plot: {e}", exc_info=True)
        return None

# Form definitions for Budget Planner
class PersonalInfoForm(FlaskForm):
    first_name = StringField('First Name', validators=[Optional()])
    email = StringField('Email', validators=[Optional(), Email()])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], validators=[DataRequired()])
    submit = SubmitField('Next')

class IncomeForm(FlaskForm):
    monthly_income = FloatField('Monthly Income', validators=[DataRequired(), non_negative])
    submit = SubmitField('Next')
    back = SubmitField('Back')

class ExpensesForm(FlaskForm):
    housing_expenses = FloatField('Housing Expenses', validators=[DataRequired(), non_negative])
    food_expenses = FloatField('Food Expenses', validators=[DataRequired(), non_negative])
    transport_expenses = FloatField('Transport Expenses', validators=[DataRequired(), non_negative])
    other_expenses = FloatField('Other Expenses', validators=[DataRequired(), non_negative])
    submit = SubmitField('Next')
    back = SubmitField('Back')

class SavingsForm(FlaskForm):
    savings_goal = FloatField('Savings Goal', validators=[DataRequired(), non_negative])
    auto_email = BooleanField('Auto Email')
    submit = SubmitField('Submit')
    back = SubmitField('Back')

# Form definition for Financial Health Tool
class HealthForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    last_name = StringField('Last Name', validators=[Optional()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    phone_number = StringField('Phone Number', validators=[Optional()])
    business_name = StringField('Business Name', validators=[DataRequired()])
    user_type = SelectField('User Type', choices=[('SME', 'SME'), ('Individual', 'Individual')], validators=[DataRequired()])
    income_revenue = FloatField('Monthly Income/Revenue', validators=[DataRequired(), non_negative])
    expenses_costs = FloatField('Monthly Expenses/Costs', validators=[DataRequired(), non_negative])
    debt_loan = FloatField('Total Debt/Loan Amount', validators=[DataRequired(), non_negative])
    debt_interest_rate = FloatField('Debt Interest Rate (%)', validators=[DataRequired(), non_negative])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], validators=[DataRequired()])
    auto_email = BooleanField('Auto Email')
    submit = SubmitField('Submit')

# Form definition for Financial Personality Quiz
class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[Optional()])
    email = StringField('Email', validators=[Optional(), Email()])
    language = SelectField('Language', choices=[('en', 'English'), ('ha', 'Hausa')], validators=[DataRequired()])
    # Dynamic quiz questions will be added as RadioFields
    submit = SubmitField('Submit Quiz')

# Quiz questions pool
QUIZ_QUESTIONS = [
    {"text": "Do you track your expenses weekly?", "type": "yes_no", "tooltip": "Tracking: Recording all money spent daily."},
    {"text": "Do you avoid budgeting?", "type": "yes_no", "tooltip": "Budgeting: Planning how to spend your money."},
    {"text": "Do you save a portion of your income monthly?", "type": "yes_no", "tooltip": "Saving: Setting aside money for future use."},
    {"text": "Do you often spend on non-essentials like entertainment?", "type": "yes_no", "tooltip": "Non-essentials: Items or activities not critical to daily needs."},
    {"text": "Do you plan your purchases in advance?", "type": "yes_no", "tooltip": "Planning: Deciding what to buy before spending."},
    {"text": "How often do you save?", "type": "multiple_choice", "options": ["Never", "Sometimes", "Always"], "tooltip": "Saving frequency: How regularly you set aside money."},
    {"text": "How often do you overspend at the market?", "type": "multiple_choice", "options": ["Never", "Sometimes", "Always"], "tooltip": "Overspending: Spending more than planned."},
    {"text": "How often do you review your finances?", "type": "multiple_choice", "options": ["Never", "Sometimes", "Always"], "tooltip": "Reviewing: Checking your income and expenses."},
    {"text": "How often do you buy on impulse?", "type": "multiple_choice", "options": ["Never", "Sometimes", "Always"], "tooltip": "Impulse buying: Purchasing without planning."},
    {"text": "How often do you join savings groups?", "type": "multiple_choice", "options": ["Never", "Sometimes", "Always"], "tooltip": "Savings groups: Local groups for collective saving."}
]

def assign_personality(answers, language='en'):
    """
    Assign financial personality based on quiz answers.
    Scoring: Higher scores for Planner/Saver, lower for Spender/Avoider.
    """
    score = 0
    for question, answer in answers:
        q = next((q for q in QUIZ_QUESTIONS if q['text'] == question), None)
        if not q:
            continue
        if q['type'] == 'yes_no':
            if q['text'] in ["Do you track your expenses weekly?", "Do you save a portion of your income monthly?", "Do you plan your purchases in advance?"]:
                score += 1 if answer == 'Yes' else -1
            elif q['text'] in ["Do you avoid budgeting?", "Do you often spend on non-essentials like entertainment?"]:
                score -= 1 if answer == 'Yes' else 1
        else:  # multiple_choice
            if answer == 'Always':
                score += 1 if q['text'] in ["How often do you save?", "How often do you review your finances?", "How often do you join savings groups?"] else -1
            elif answer == 'Never':
                score -= 1 if q['text'] in ["How often do you save?", "How often do you review your finances?", "How often do you join savings groups?"] else 1
            # 'Sometimes' is neutral (score += 0)
    
    if score >= 3:
        return 'Planner', translations[language]['Planner'], translations[language]['Planner Tip']
    elif score >= 1:
        return 'Saver', translations[language]['Saver'], translations[language]['Saver Tip']
    elif score == 0:
        return 'Minimalist', translations[language]['Minimalist'], translations[language]['Minimalist Tip']
    elif score >= -2:
        return 'Spender', translations[language]['Spender'], translations[language]['Spender Tip']
    else:
        return 'Avoider', translations[language]['Avoider'], translations[language]['Avoider Tip']

def generate_quiz_summary_chart(answers, language='en'):
    """
    Generate a bar chart summarizing quiz answers.
    """
    try:
        answer_counts = {'Yes': 0, 'No': 0, 'Always': 0, 'Sometimes': 0, 'Never': 0}
        for _, answer in answers:
            if answer in answer_counts:
                answer_counts[answer] += 1
        labels = list(answer_counts.keys())
        values = list(answer_counts.values())
        fig = px.bar(
            x=labels,
            y=values,
            title=translations[language]['Quiz Summary'],
            labels={'x': 'Answer', 'y': 'Count'}
        )
        fig.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=300,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)'
        )
        return fig.to_html(full_html=False, include_plotlyjs=False)
    except Exception as e:
        logger.error(f"Error generating quiz summary chart: {e}")
        return None

def send_quiz_email(to_email, user_name, personality, personality_desc, tip, language):
    """
    Send quiz results via email.
    """
    try:
        if language not in translations:
            logger.warning(f"Invalid language '{language}' for email to {to_email}. Defaulting to English.")
            language = 'en'
        subject = translations[language]['Quiz Report Subject']
        msg = Message(
            subject=subject,
            recipients=[to_email],
            html=render_template(
                'quiz_email.html',
                trans=translations[language],
                user_name=sanitize_input(user_name) or 'User',
                personality=personality,
                personality_desc=personality_desc,
                tip=tip,
                FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
                WAITLIST_FORM_URL=WAITLIST_FORM_URL,
                CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
                linkedin_url=LINKEDIN_URL,
                twitter_url=TWITTER_URL,
                language=language
            )
        )
        mail.send(msg)
        logger.info(f"Quiz email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error sending quiz email to {to_email}: {e}")
        return False

def send_quiz_email_async(to_email, user_name, personality, personality_desc, tip, language):
    """
    Send quiz email asynchronously.
    """
    try:
        with app.app_context():
            send_quiz_email(to_email, user_name, personality, personality_desc, tip, language)
    except Exception as e:
        logger.error(f"Async quiz email sending failed for {to_email}: {e}", exc_info=True)

# Routes
@app.route('/')
def index():
    tool = request.args.get('tool', 'budget')
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    return render_template(
        'index.html',
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        tool=tool,
        language=language
    )

@app.route('/personal_info', methods=['GET', 'POST'])
def personal_info():
    form = PersonalInfoForm()
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if form.validate_on_submit():
        session['budget_data'] = {
            'first_name': sanitize_input(form.first_name.data),
            'email': sanitize_input(form.email.data),
            'language': form.language.data
        }
        session['language'] = form.language.data
        session.modified = True
        logger.info(f"Personal info saved in session: {session['budget_data']}")
        return redirect(url_for('income'))
    return render_template(
        'personal_info.html',
        form=form,
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        step=1,
        language=language
    )

@app.route('/income', methods=['GET', 'POST'])
def income():
    form = IncomeForm()
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if 'budget_data' not in session:
        flash(translations[language]['Session Expired'], 'error')
        return redirect(url_for('personal_info'))
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('personal_info'))
        session['budget_data']['monthly_income'] = form.monthly_income.data
        session.modified = True
        logger.info(f"Income saved in session: {form.monthly_income.data}")
        return redirect(url_for('expenses'))
    return render_template(
        'income.html',
        form=form,
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        step=2,
        language=language
    )

@app.route('/expenses', methods=['GET', 'POST'])
def expenses():
    form = ExpensesForm()
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if 'budget_data' not in session:
        flash(translations[language]['Session Expired'], 'error')
        return redirect(url_for('personal_info'))
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('income'))
        session['budget_data'].update({
            'housing_expenses': form.housing_expenses.data,
            'food_expenses': form.food_expenses.data,
            'transport_expenses': form.transport_expenses.data,
            'other_expenses': form.other_expenses.data
        })
        session.modified = True
        logger.info(f"Expenses saved in session: {session['budget_data']}")
        return redirect(url_for('savings'))
    return render_template(
        'expenses.html',
        form=form,
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        step=3,
        language=language
    )

@app.route('/savings', methods=['GET', 'POST'])
def savings():
    form = SavingsForm()
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if 'budget_data' not in session:
        flash(translations[language]['Session Expired'], 'error')
        return redirect(url_for('personal_info'))
    if form.validate_on_submit():
        if form.back.data:
            return redirect(url_for('expenses'))
        session['budget_data'].update({
            'savings_goal': form.savings_goal.data,
            'auto_email': form.auto_email.data
        })
        session.modified = True
        logger.info(f"Savings data saved in session: {session['budget_data']}")
        return redirect(url_for('budget_dashboard'))
    return render_template(
        'savings.html',
        form=form,
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        step=4,
        language=language
    )

@app.route('/budget_dashboard', methods=['GET', 'POST'])
def budget_dashboard():
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if 'budget_data' not in session:
        flash(translations[language]['Session Expired'], 'error')
        return redirect(url_for('personal_info'))
    budget_data = session['budget_data']
    email = budget_data.get('email', '')
    try:
        df = pd.DataFrame([budget_data], columns=PREDETERMINED_HEADERS_BUDGET)
        df = calculate_budget_metrics(df)
        if df.empty:
            flash(translations[language]['Error retrieving data. Please try again.'], 'error')
            return redirect(url_for('personal_info'))
        user_df = df
        badges = assign_badges_budget(user_df)
        user_df['badges'] = ', '.join(badges)
        data = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            budget_data.get('first_name', ''),
            email,
            language,
            budget_data.get('monthly_income', 0),
            budget_data.get('housing_expenses', 0),
            budget_data.get('food_expenses', 0),
            budget_data.get('transport_expenses', 0),
            budget_data.get('other_expenses', 0),
            budget_data.get('savings_goal', 0),
            budget_data.get('auto_email', False),
            user_df['total_expenses'].iloc[0],
            user_df['savings'].iloc[0],
            user_df['surplus_deficit'].iloc[0],
            user_df['badges'].iloc[0],
            0,  # Rank placeholder
            0   # Total users placeholder
        ]
        if append_to_sheet(data, PREDETERMINED_HEADERS_BUDGET, 'Budget'):
            logger.info(f"Budget data appended to Google Sheets for email: {email}")
        else:
            flash(translations[language]['Google Sheets Error'], 'error')
            return redirect(url_for('personal_info'))
        flash(translations[language]['Submission Success'], 'success')
        return render_template(
            'budget_dashboard.html',
            user_df=user_df,
            trans=translations[language],
            badges=badges,
            FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
            WAITLIST_FORM_URL=WAITLIST_FORM_URL,
            CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
            linkedin_url=LINKEDIN_URL,
            twitter_url=TWITTER_URL,
            language=language
        )
    except Exception as e:
        logger.error(f"Error in budget_dashboard: {e}")
        flash(translations[language]['Error retrieving data. Please try again.'], 'error')
        return redirect(url_for('personal_info'))

@app.route('/health', methods=['GET', 'POST'])
def health():
    form = HealthForm()
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if form.validate_on_submit():
        health_data = {
            'first_name': sanitize_input(form.first_name.data),
            'last_name': sanitize_input(form.last_name.data),
            'email': sanitize_input(form.email.data),
            'phone_number': sanitize_input(form.phone_number.data),
            'business_name': sanitize_input(form.business_name.data),
            'user_type': form.user_type.data,
            'income_revenue': form.income_revenue.data,
            'expenses_costs': form.expenses_costs.data,
            'debt_loan': form.debt_loan.data,
            'debt_interest_rate': form.debt_interest_rate.data,
            'language': form.language.data,
            'auto_email': form.auto_email.data
        }
        session['health_data'] = health_data
        session['language'] = form.language.data
        session.modified = True
        logger.info(f"Health data saved in session: {health_data}")
        return redirect(url_for('health_dashboard'))
    return render_template(
        'health.html',
        form=form,
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        language=language
    )

@app.route('/health_dashboard', methods=['GET', 'POST'])
def health_dashboard():
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if 'health_data' not in session:
        flash(translations[language]['Session data missing. Please submit again.'], 'error')
        return redirect(url_for('health'))
    health_data = session['health_data']
    email = health_data.get('email', '')
    try:
        df = pd.DataFrame([health_data], columns=PREDETERMINED_HEADERS_HEALTH)
        df = calculate_health_score(df)
        if df.empty:
            flash(translations[language]['Error retrieving data. Please try again.'], 'error')
            return redirect(url_for('health'))
        user_df = df
        all_users_df = fetch_data_from_sheet(worksheet_name='Health', headers=PREDETERMINED_HEADERS_HEALTH)
        badges = assign_badges_health(user_df, all_users_df)
        user_df['badges'] = ', '.join(badges)
        rank = sum(all_users_df['HealthScore'] > user_df['HealthScore'].iloc[0]) + 1
        total_users = len(all_users_df)
        breakdown_plot = generate_breakdown_plot(user_df)
        comparison_plot = generate_comparison_plot(user_df, all_users_df)
        if not breakdown_plot or not comparison_plot:
            flash(translations[language]['Error generating plots. Dashboard will display without plots.'], 'warning')
        data = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            health_data.get('business_name', ''),
            health_data.get('income_revenue', 0),
            health_data.get('expenses_costs', 0),
            health_data.get('debt_loan', 0),
            health_data.get('debt_interest_rate', 0),
            health_data.get('auto_email', False),
            health_data.get('phone_number', ''),
            health_data.get('first_name', ''),
            health_data.get('last_name', ''),
            health_data.get('user_type', ''),
            email,
            user_df['badges'].iloc[0],
            language
        ]
        if append_to_sheet(data, PREDETERMINED_HEADERS_HEALTH, 'Health'):
            logger.info(f"Health data appended to Google Sheets for email: {email}")
        else:
            flash(translations[language]['Google Sheets Error'], 'error')
            return redirect(url_for('health'))
        if health_data.get('auto_email') and email:
            threading.Thread(
                target=send_health_email_async,
                args=(
                    email,
                    health_data.get('first_name', 'User'),
                    user_df['HealthScore'].iloc[0],
                    user_df['ScoreDescription'].iloc[0],
                    rank,
                    total_users,
                    user_df['CourseTitle'].iloc[0],
                    user_df['CourseURL'].iloc[0],
                    language
                )
            ).start()
            flash(translations[language]['Check Inbox'], 'success')
        flash(translations[language]['Submission Success'], 'success')
        return render_template(
            'health_dashboard.html',
            user_df=user_df,
            trans=translations[language],
            badges=badges,
            rank=rank,
            total_users=total_users,
            breakdown_plot=breakdown_plot,
            comparison_plot=comparison_plot,
            FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
            WAITLIST_FORM_URL=WAITLIST_FORM_URL,
            CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
            linkedin_url=LINKEDIN_URL,
            twitter_url=TWITTER_URL,
            language=language
        )
    except Exception as e:
        logger.error(f"Error in health_dashboard: {e}")
        flash(translations[language]['An unexpected error occurred. Please try again.'], 'error')
        return redirect(url_for('health'))

@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    # Dynamically create form with 5 random questions
    form = QuizForm()
    if 'quiz_questions' not in session:
        selected_questions = random.sample(QUIZ_QUESTIONS, 5)
        session['quiz_questions'] = selected_questions
        session.modified = True
    else:
        selected_questions = session['quiz_questions']
    # Add dynamic RadioFields for quiz questions
    for i, q in enumerate(selected_questions, 1):
        choices = [('Yes', translations[language]['Yes']), ('No', translations[language]['No'])] if q['type'] == 'yes_no' else [(opt, opt) for opt in q['options']]
        setattr(QuizForm, f'question_{i}', RadioField(q['text'], choices=choices, validators=[DataRequired()]))
    form.process()  # Re-process form to include dynamic fields
    if form.validate_on_submit():
        quiz_data = {
            'first_name': sanitize_input(form.first_name.data),
            'email': sanitize_input(form.email.data),
            'language': form.language.data,
            'auto_email': False  # Auto-email not implemented in form yet
        }
        answers = []
        for i, q in enumerate(selected_questions, 1):
            answer = getattr(form, f'question_{i}').data
            quiz_data[f'question_{i}'] = q['text']
            quiz_data[f'answer_{i}'] = answer
            answers.append((q['text'], answer))
        personality, personality_desc, tip = assign_personality(answers, form.language.data)
        quiz_data['personality'] = personality
        quiz_data['badges'] = translations[form.language.data]['Personality Unlocked!']
        # Save to Google Sheets
        data = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            quiz_data.get('first_name', ''),
            quiz_data.get('email', ''),
            quiz_data.get('language', 'en'),
            quiz_data.get('question_1', ''),
            quiz_data.get('answer_1', ''),
            quiz_data.get('question_2', ''),
            quiz_data.get('answer_2', ''),
            quiz_data.get('question_3', ''),
            quiz_data.get('answer_3', ''),
            quiz_data.get('question_4', ''),
            quiz_data.get('answer_4', ''),
            quiz_data.get('question_5', ''),
            quiz_data.get('answer_5', ''),
            quiz_data.get('personality', ''),
            quiz_data.get('badges', ''),
            quiz_data.get('auto_email', False)
        ]
        if append_to_sheet(data, PREDETERMINED_HEADERS_QUIZ, 'Quiz'):
            logger.info(f"Quiz data appended to Google Sheets for email: {quiz_data.get('email', '')}")
        else:
            flash(translations[language]['Google Sheets Error'], 'error')
            return redirect(url_for('quiz'))
        # Generate summary chart
        summary_chart = generate_quiz_summary_chart(answers, form.language.data)
        session['quiz_results'] = {
            'first_name': quiz_data.get('first_name', ''),
            'email': quiz_data.get('email', ''),
            'language': quiz_data.get('language', 'en'),
            'personality': personality,
            'personality_desc': personality_desc,
            'tip': tip,
            'answers': answers,
            'badges': [quiz_data['badges']],
            'summary_chart': summary_chart
        }
        session.pop('quiz_questions', None)  # Clear questions for retake
        session.modified = True
        if quiz_data.get('email') and quiz_data.get('auto_email'):
            threading.Thread(
                target=send_quiz_email_async,
                args=(
                    quiz_data.get('email'),
                    quiz_data.get('first_name', 'User'),
                    personality,
                    personality_desc,
                    tip,
                    form.language.data
                )
            ).start()
            flash(translations[language]['Check Inbox'], 'success')
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('quiz_results'))
    return render_template(
        'quiz.html',
        form=form,
        questions=selected_questions,
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        language=language
    )

@app.route('/quiz_results', methods=['GET', 'POST'])
def quiz_results():
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
        session['language'] = language
    if 'quiz_results' not in session:
        flash(translations[language]['Session data missing. Please submit again.'], 'error')
        return redirect(url_for('quiz'))
    quiz_results = session['quiz_results']
    return render_template(
        'quiz_results.html',
        results=quiz_results,
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        language=language
    )

@app.errorhandler(404)
def page_not_found(e):
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
    return render_template(
        '404.html',
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        language=language
    ), 404

@app.errorhandler(500)
def internal_server_error(e):
    language = session.get('language', 'en')
    if language not in translations:
        language = 'en'
    logger.error(f"500 error: {str(e)}")
    return render_template(
        '500.html',
        trans=translations[language],
        FEEDBACK_FORM_URL=FEEDBACK_FORM_URL,
        WAITLIST_FORM_URL=WAITLIST_FORM_URL,
        CONSULTANCY_FORM_URL=CONSULTANCY_FORM_URL,
        linkedin_url=LINKEDIN_URL,
        twitter_url=TWITTER_URL,
        language=language
    ), 500

if __name__ == '__main__':
    app.run(debug=True)
