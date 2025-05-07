import os
import sys
import uuid
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, TextAreaField, EmailField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Email, Optional, NumberRange, EqualTo
from flask_mail import Mail, Message
from smtplib import SMTPException, SMTPAuthenticationError
import gspread
from google.oauth2.service_account import Credentials
from dateutil.parser import parse
import numpy as np
import plotly.graph_objects as go
import plotly.io as pio
from flask_caching import Cache
from celery import Celery
from celery.schedules import crontab
import redis
import atexit
from math import ceil
from translations import translations

# Configure logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError as e:
    logger.error("Failed to load environment variables: %s", e)
    sys.exit(1)

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app_secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app_secret_key:
    logger.error("FLASK_SECRET_KEY environment variable not set")
    sys.exit(1)
app.secret_key = app_secret_key
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)
app.config['WTF_CSRF_ENABLED'] = True

# Configure Flask-Caching
cache_config = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300
}
app.config.from_mapping(cache_config)
cache = Cache(app)

# Configure Celery
celery_broker_url = os.environ.get('CELERY_BROKER_URL')
celery_result_backend = os.environ.get('CELERY_RESULT_BACKEND')
if not celery_broker_url or not celery_result_backend:
    logger.error("CELERY_BROKER_URL or CELERY_RESULT_BACKEND environment variable not set")
    sys.exit(1)
app.config['CELERY_BROKER_URL'] = celery_broker_url
app.config['CELERY_RESULT_BACKEND'] = celery_result_backend
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)
celery.conf.beat_schedule = {
    'check-bill-reminders-every-minute': {
        'task': 'app.check_bill_reminders',
        'schedule': crontab(minute='*'),
    },
}

# Configure Flask-Mail
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'ficore.ai.africa@gmail.com'
app.config['MAIL_PASSWORD'] = os.environ.get('SMTP_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = 'ficore.ai.africa@gmail.com'
app.config['MAIL_ENABLED'] = bool(app.config['MAIL_PASSWORD'])
if not app.config['MAIL_ENABLED']:
    logger.warning("SMTP_PASSWORD not set in environment. Email functionality will be disabled.")
try:
    mail = Mail(app)
    logger.info("Flask-Mail initialized successfully.")
except Exception as e:
    logger.error(f"Failed to initialize Flask-Mail: {str(e)}")
    
# Initialize Google Sheets client with google-auth
scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
try:
    creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if not creds_json:
        logger.error("GOOGLE_CREDENTIALS_JSON environment variable not set")
        sys.exit(1)
    
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(creds)
except json.JSONDecodeError as e:
    logger.error(f"Invalid GOOGLE_CREDENTIALS_JSON format: {e}")
    sys.exit(1)
except Exception as e:
    logger.error(f"Failed to initialize Google Sheets credentials: {e}")
    sys.exit(1)

# Open the new Google Sheet
try:
    spreadsheet = client.open_by_key('13hbiMTMRBHo9MHjWwcugngY_aSiuxII67HCf03MiZ8I')
except Exception as e:
    logger.error(f"Error accessing Google Sheets: {e}")
    sys.exit(1)

# Define worksheet configurations
WORKSHEETS = {
    'Authentication': {
        'name': 'AuthenticationSheet',
        'headers': ['Timestamp', 'FirstName', 'Email', 'LastName', 'PhoneNumber', 'Language', 'SessionID']
    },
    'HealthScore': {
        'name': 'HealthScoreSheet',
        'headers': ['Timestamp', 'BusinessName', 'IncomeRevenue', 'ExpensesCosts', 'DebtLoan', 'DebtInterestRate', 'AutoEmail', 'PhoneNumber', 'FirstName', 'LastName', 'UserType', 'Email', 'Badges', 'Language', 'Score']
    },
    'NetWorth': {
        'name': 'NetWorthSheet',
        'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Assets', 'Liabilities', 'NetWorth']
    },
    'Quiz': {
        'name': 'QuizSheet',
        'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'QuizScore', 'Personality']
    },
    'EmergencyFund': {
        'name': 'EmergencyFundSheet',
        'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'MonthlyExpenses', 'RecommendedFund']
    },
    'Budget': {
        'name': 'BudgetSheet',
        'headers': ['Timestamp', 'FirstName', 'Email', 'AutoEmail', 'Language', 'MonthlyIncome', 'HousingExpenses', 'FoodExpenses', 'TransportExpenses', 'OtherExpenses', 'TotalExpenses', 'Savings', 'SurplusDeficit']
    },
    'ExpenseTracker': {
        'name': 'ExpenseTrackerSheet',
        'headers': ['ID', 'UserEmail', 'Amount', 'Category', 'Date', 'Description', 'Timestamp', 'TransactionType', 'RunningBalance']
    },
    'BillPlanner': {
        'name': 'BillPlannerSheet',
        'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Description', 'Amount', 'DueDate', 'Category', 'Recurrence', 'Status', 'SendEmail']
    },
    'BillReminders': {
        'name': 'BillRemindersSheet',
        'headers': ['Timestamp', 'BillTimestamp', 'Email', 'ReminderDate', 'Status']
    }
}

# Initialize worksheets and ensure headers
sheets = {}
def initialize_worksheet(tool):
    config = WORKSHEETS[tool]
    try:
        sheet = spreadsheet.worksheet(config['name'])
    except gspread.exceptions.WorksheetNotFound:
        logger.info(f"Creating new worksheet: {config['name']}")
        sheet = spreadsheet.add_worksheet(title=config['name'], rows=100, cols=len(config['headers']))
        sheet.append_row(config['headers'])
    try:
        current_headers = sheet.row_values(1)
        if not current_headers or current_headers != config['headers']:
            logger.info(f"Updating headers for {config['name']}")
            sheet.clear()
            sheet.append_row(config['headers'])
    except Exception as e:
        logger.error(f"Error setting headers for {config['name']}: {e}")
        sheet.clear()
        sheet.append_row(config['headers'])
    return sheet

for tool in WORKSHEETS:
    sheets[tool] = initialize_worksheet(tool)

# Utility function to parse numbers with comma support
def parse_number(value):
    try:
        if isinstance(value, str):
            value = value.replace(',', '')
        return float(value)
    except (ValueError, TypeError):
        return 0

# Safe translation access with fallback to English
def get_translation(key, language='English'):
    try:
        return translations.get(language, translations['English'])[key]
    except KeyError:
        logger.warning(f"Translation key '{key}' not found for language '{language}', falling back to English")
        return translations['English'].get(key, f"Missing translation: {key}")

# Store authentication data
def store_authentication_data(form_data):
    language = session.get('language', 'English')
    try:
        auth_data = {
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'FirstName': form_data.get('first_name', ''),
            'Email': form_data.get('email', ''),
            'LastName': form_data.get('last_name', ''),
            'PhoneNumber': form_data.get('phone', ''),
            'Language': form_data.get('language', language),
            'SessionID': session.get('session_id', str(uuid.uuid4()))
        }
        session['session_id'] = auth_data['SessionID']
        update_or_append_user_data(auth_data, 'Authentication')
    except Exception as e:
        logger.error(f"Error storing authentication data: {e}")
        flash(get_translation('Failed to store authentication data', language), 'error')

# Fetch user data by email with validation
def get_user_data_by_email(email, tool):
    try:
        sheet = sheets[tool]
        records = sheet.get_all_records()
        user_records = []
        for record in records:
            if not isinstance(record, dict):
                logger.warning(f"Malformed record in {tool}: {record}")
                continue
            if record.get('Email') == email or record.get('UserEmail') == email:
                user_records.append(record)
        return user_records
    except Exception as e:
        logger.error(f"Error fetching user data from {WORKSHEETS[tool]['name']}: {e}")
        return []

# Fetch record by ID
def get_record_by_id(id, tool):
    try:
        sheet = sheets[tool]
        records = sheet.get_all_records()
        for record in records:
            if not isinstance(record, dict):
                logger.warning(f"Malformed record in {tool}: {record}")
                continue
            if record.get('ID') == id or record.get('Timestamp') == id or record.get('BillTimestamp') == id:
                return record
        return None
    except Exception as e:
        logger.error(f"Error fetching record by ID from {WORKSHEETS[tool]['name']}: {e}")
        return None

# Update or append user data to Google Sheets
def update_or_append_user_data(user_data, tool, update_only_specific_fields=None):
    language = session.get('language', 'English')
    sheet = sheets[tool]
    headers = WORKSHEETS[tool]['headers']
    try:
        records = sheet.get_all_records()
        email = user_data.get('Email') or user_data.get('UserEmail')
        id = user_data.get('ID') or user_data.get('Timestamp') or user_data.get('BillTimestamp')
        found = False
        for i, record in enumerate(records, start=2):
            if not isinstance(record, dict):
                logger.warning(f"Malformed record in {tool}: {record}")
                continue
            if record.get('Email') == email or record.get('UserEmail') == email or record.get('ID') == id or record.get('Timestamp') == id or record.get('BillTimestamp') == id:
                if update_only_specific_fields:
                    merged_data = {**record}
                    for field in update_only_specific_fields:
                        if field in user_data:
                            merged_data[field] = user_data[field]
                else:
                    merged_data = {**record, **user_data}
                sheet.update(f'A{i}:{chr(64 + len(headers))}{i}', [[merged_data.get(header, '') for header in headers]])
                found = True
                break
        if not found:
            sheet.append_row([user_data.get(header, '') for header in headers])
    except gspread.exceptions.APIError as e:
        logger.error(f"Google Sheets API error updating/appending data to {WORKSHEETS[tool]['name']}: {e}")
        flash(get_translation('Failed to save data due to Google Sheets API limit', language), 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])
    except Exception as e:
        logger.error(f"Error updating/appending data to {WORKSHEETS[tool]['name']}: {e}")
        flash(get_translation('Failed to save data due to server error', language), 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])

# Calculate running balance for expense tracker (optimized)
def calculate_running_balance(email):
    try:
        sheet = sheets['ExpenseTracker']
        records = sheet.get_all_records()
        user_records = [r for r in records if r.get('UserEmail') == email]
        if not user_records:
            return 0
        sorted_records = sorted(user_records, key=lambda x: parse(x.get('Timestamp', '1970-01-01 00:00:00')))
        balance = 0
        for i, record in enumerate(sorted_records):
            amount = parse_number(record.get('Amount', 0))
            balance += amount if record.get('TransactionType') == 'Income' else -amount
            if i == len(sorted_records) - 1:  # Update only the latest record
                record['RunningBalance'] = balance
                update_or_append_user_data(record, 'ExpenseTracker', update_only_specific_fields=['RunningBalance'])
        return balance
    except Exception as e:
        logger.error(f"Error calculating running balance: {e}")
        return 0

# Assign net worth rank with caching
@cache.memoize(timeout=300)
def assign_net_worth_rank(net_worth):
    try:
        sheet = sheets['NetWorth']
        all_net_worths = [parse_number(row.get('NetWorth', 0)) for row in sheet.get_all_records() if row.get('NetWorth')]
        all_net_worths.append(net_worth)
        rank_percentile = 100 - np.percentile(all_net_worths, np.searchsorted(sorted(all_net_worths, reverse=True), net_worth) / len(all_net_worths) * 100)
        return round(rank_percentile, 1)
    except Exception as e:
        logger.error(f"Error assigning net worth rank: {e}")
        return 50.0

# Get net worth advice
def get_net_worth_advice(net_worth, language='English'):
    if net_worth > 0:
        return get_translation('Maintain your positive net worth by continuing to manage liabilities and grow assets.', language)
    elif net_worth == 0:
        return get_translation('Your net worth is balanced. Consider increasing assets to build wealth.', language)
    else:
        return get_translation('Focus on reducing liabilities to improve your net worth.', language)

# Assign net worth badges
def assign_net_worth_badges(net_worth, language='English'):
    badges = []
    try:
        if net_worth > 0:
            badges.append(get_translation('Positive Net Worth', language))
        if net_worth >= 100000:
            badges.append(get_translation('Wealth Builder', language))
        if net_worth <= -50000:
            badges.append(get_translation('Debt Recovery', language))
    except Exception as e:
        logger.error(f"Error assigning net worth badges: {e}")
    return badges

# Get financial tips
def get_tips(language='English'):
    default_tips = [
        'Regularly review your assets and liabilities to track progress.',
        'Invest in low-risk assets to grow your wealth steadily.',
        'Create a plan to pay down high-interest debt first.'
    ]
    return [get_translation(tip, language) for tip in default_tips]

# Get recommended courses
def get_courses(language='English'):
    return [
        {'title': get_translation('Personal Finance 101', language), 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': get_translation('Debt Management Basics', language), 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': get_translation('Investing for Beginners', language), 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'}
    ]

# Get quiz advice
def get_quiz_advice(score, personality, language='English'):
    if score >= 4:
        return get_translation('Great job! Continue to leverage your {personality} approach to build wealth.', language).format(personality=personality.lower())
    elif score >= 2:
        return get_translation('Good effort! Your {personality} style is solid, but consider tracking expenses more closely.', language).format(personality=personality.lower())
    else:
        return get_translation('Keep learning! Your {personality} approach can improve with regular financial reviews.', language).format(personality=personality.lower())

# Assign quiz badges
def assign_quiz_badges(score, language='English'):
    badges = []
    try:
        if score >= 4:
            badges.append(get_translation('Financial Guru', language))
        if score >= 2:
            badges.append(get_translation('Quiz Achiever', language))
        badges.append(get_translation('Quiz Participant', language))
    except Exception as e:
        logger.error(f"Error assigning quiz badges: {e}")
    return badges

# Get average health score with caching
@cache.memoize(timeout=300)
def get_average_health_score():
    try:
        sheet = sheets['HealthScore']
        records = sheet.get_all_records()
        scores = [parse_number(row.get('Score', 0)) for row in records if row.get('Score') is not None]
        return np.mean(scores) if scores else 50
    except Exception as e:
        logger.error(f"Error calculating average health score: {e}")
        return 50

# Generate health score charts with caching
@cache.memoize(timeout=300)
def generate_health_score_charts(income_revenue, debt_loan, health_score, average_score, language):
    """
    Generate Plotly charts for health score visualization.
    Args:
        income_revenue (float): Money received monthly.
        debt_loan (float): Total money owed.
        health_score (float): User's financial health score.
        average_score (float): Average health score of all users.
        language (str): Language for translations.
    Returns:
        tuple: HTML strings for asset chart and comparison chart.
    """
    translations = {
        'English': {
            'Money You Get': 'Money You Get',
            'Money You Owe': 'Money You Owe',
            'Your Score': 'Your Score',
            'Average Score': 'Average Score',
            'Financial Health': 'Financial Health'
        },
        'Hausa': {
            'Money You Get': 'Kuɗin da Kuke Samu',
            'Money You Owe': 'Kuɗin da Kuke Bin Bashi',
            'Your Score': 'Makin Ku',
            'Average Score': 'Matsakaicin Maki',
            'Financial Health': 'Lafiyar Kuɗi'
        }
    }
    
    # Asset vs. Liability Chart
    fig1 = go.Figure(data=[
        go.Bar(
            x=[translations[language]['Money You Get'], translations[language]['Money You Owe']],
            y=[income_revenue, debt_loan],
            marker_color=['#2E7D32', '#D32F2F']
        )
    ])
    fig1.update_layout(
        title=translations[language]['Financial Health'],
        yaxis_title='Amount (₦)',
        showlegend=False,
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)'
    )
    chart_html = to_html(fig1, include_plotlyjs=True, full_html=False)
    
    # Score Comparison Chart
    fig2 = go.Figure(data=[
        go.Bar(
            x=[translations[language]['Your Score'], translations[language]['Average Score']],
            y=[health_score, average_score],
            marker_color=['#0288D1', '#FFB300']
        )
    ])
    fig2.update_layout(
        title=translations[language]['Financial Health'],
        yaxis_title='Score',
        showlegend=False,
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)'
    )
    comparison_chart_html = to_html(fig2, include_plotlyjs=False, full_html=False)
    
    return chart_html, comparison_chart_html
    
# Generate net worth charts with caching
@cache.memoize(timeout=300)
def generate_net_worth_charts(net_worth_data_json, language='English'):
    net_worth_data = json.loads(net_worth_data_json)
    try:
        labels = [get_translation('Assets', language), get_translation('Liabilities', language)]
        values = [parse_number(net_worth_data.get('Assets', 0)), parse_number(net_worth_data.get('Liabilities', 0))]
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545']))])
        pie_fig.update_layout(
            title=get_translation('Asset-Liability Breakdown', language),
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            hovermode='closest'
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=True)

        sheet = sheets['NetWorth']
        all_net_worths = [parse_number(row.get('NetWorth', 0)) for row in sheet.get_all_records() if row.get('NetWorth')]
        user_net_worth = parse_number(net_worth_data.get('NetWorth', 0))
        avg_net_worth = np.mean(all_net_worths) if all_net_worths else 0
        bar_fig = go.Figure(data=[
            go.Bar(name=get_translation('Your Net Worth', language), x=['You'], y=[user_net_worth], marker_color='#2E7D32'),
            go.Bar(name=get_translation('Average Net Worth', language), x=['Average'], y=[avg_net_worth], marker_color='#0288D1')
        ])
        bar_fig.update_layout(
            title=get_translation('Comparison to Peers', language),
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            hovermode='closest'
        )
        comparison_chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=True)
        return chart_html, comparison_chart_html
    except Exception as e:
        logger.error(f"Error generating net worth charts: {e}")
        return get_translation('Chart failed to load. Please try again.', language), ""

# Generate budget charts with caching
@cache.memoize(timeout=300)
def generate_budget_charts(budget_data_json, language='English'):
    budget_data = json.loads(budget_data_json)
    try:
        labels = [
            get_translation('Housing', language),
            get_translation('Food', language),
            get_translation('Transport', language),
            get_translation('Other', language),
            get_translation('Savings', language)
        ]
        values = [
            parse_number(budget_data.get('HousingExpenses', 0)),
            parse_number(budget_data.get('FoodExpenses', 0)),
            parse_number(budget_data.get('TransportExpenses', 0)),
            parse_number(budget_data.get('OtherExpenses', 0)),
            max(parse_number(budget_data.get('Savings', 0)), 0)
        ]
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545', '#0288D1', '#FFB300', '#4CAF50']))])
        pie_fig.update_layout(
            title=get_translation('Budget Breakdown', language),
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            hovermode='closest'
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=True)
        return chart_html
    except Exception as e:
        logger.error(f"Error generating budget charts: {e}")
        return get_translation('Chart failed to load. Please try again.', language)

# Generate expense charts with caching
@cache.memoize(timeout=300)
def generate_expense_charts(email, language='English'):
    try:
        sheet = sheets['ExpenseTracker']
        records = sheet.get_all_records()
        user_records = [r for r in records if r.get('UserEmail') == email]
        categories = {}
        for record in user_records:
            category = record.get('Category', 'Other')
            amount = parse_number(record.get('Amount', 0))
            transaction_type = record.get('TransactionType', 'Expense')
            sign = 1 if transaction_type == 'Income' else -1
            categories[category] = categories.get(category, 0) + (sign * amount)
        labels = list(categories.keys())
        values = [abs(v) for v in categories.values()]
        if not labels:
            return get_translation('No expense data available.', language)
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545', '#0288D1', '#FFB300', '#4CAF50', '#9C27B0']))])
        pie_fig.update_layout(
            title=get_translation('Expense Breakdown by Category', language),
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            hovermode='closest'
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=True)
        return chart_html
    except Exception as e:
        logger.error(f"Error generating expense charts: {e}")
        return get_translation('Chart failed to load. Please try again.', language)

# Form definitions with enhanced UI features
class HealthScoreForm(FlaskForm):
    first_name = StringField(
        'First Name',
        validators=[DataRequired()],
        render_kw={
            'placeholder': 'Enter your first name',
            'aria-label': 'Your first name',
            'data-tooltip': 'Your first name, like John or Aisha.'
        }
    )
    last_name = StringField(
        'Last Name',
        validators=[Optional()],
        render_kw={
            'placeholder': 'Enter your last name (optional)',
            'aria-label': 'Your last name',
            'data-tooltip': 'Your last name, like Okeke or Musa (you can skip this).'
        }
    )
    email = EmailField(
        'Email',
        validators=[DataRequired(), Email()],
        render_kw={
            'placeholder': 'Enter your email',
            'aria-label': 'Your email address',
            'data-tooltip': 'Your email, like example@gmail.com, to get your score.'
        }
    )
    confirm_email = EmailField(
        'Confirm Email',
        validators=[DataRequired(), Email(), EqualTo('email', message='Emails must match')],
        render_kw={
            'placeholder': 'Re-enter your email',
            'aria-label': 'Confirm your email address',
            'data-tooltip': 'Type your email again to make sure it’s correct.'
        }
    )
    phone_number = StringField(
        'Phone Number',
        validators=[Optional()],
        render_kw={
            'placeholder': 'Enter your phone number (optional)',
            'aria-label': 'Your phone number',
            'data-tooltip': 'Your mobile number, like 08012345678 (you can skip this).'
        }
    )
    language = SelectField(
        'Language',
        choices=[('English', 'English'), ('Hausa', 'Hausa')],
        validators=[DataRequired()],
        render_kw={
            'aria-label': 'Select your language',
            'data-tooltip': 'Choose English or Hausa for the form.'
        }
    )
    business_name = StringField(
        'Business Name',
        validators=[DataRequired()],
        render_kw={
            'placeholder': 'Type personal name if no business',
            'aria-label': 'Your business or personal name',
            'data-tooltip': 'Name of your business, or your name if you don’t have a business.'
        }
    )
    user_type = SelectField(
        'User Type',
        choices=[('Individual', 'Individual'), ('Business', 'Business')],
        validators=[DataRequired()],
        render_kw={
            'aria-label': 'Are you an individual or business?',
            'data-tooltip': 'Choose Individual if it’s just you, or Business if you have a shop or company.'
        }
    )
    income_revenue = FloatField(
        'Money You Get Every Month (₦)',
        validators=[DataRequired(), NumberRange(min=0, max=10000000000)],
        render_kw={
            'placeholder': 'e.g. 150,000',
            'aria-label': 'Money you get every month',
            'data-tooltip': 'All money you receive, like salary, sales from your shop, gifts, or side jobs.'
        }
    )
    expenses_costs = FloatField(
        'Money You Spend Every Month (₦)',
        validators=[DataRequired(), NumberRange(min=0, max=10000000000)],
        render_kw={
            'placeholder': 'e.g. 60,000',
            'aria-label': 'Money you spend every month',
            'data-tooltip': 'Money you spend on things like food, rent, transport, bills, or taxes.'
        }
    )
    debt_loan = FloatField(
        'Money You Owe (₦)',
        validators=[DataRequired(), NumberRange(min=0, max=10000000000)],
        render_kw={
            'placeholder': 'e.g. 25,000',
            'aria-label': 'Money you owe',
            'data-tooltip': 'Money you borrowed from friends, family, or a bank loan you need to pay back.'
        }
    )
    debt_interest_rate = FloatField(
        'Extra Percentage on Money You Owe (%)',
        validators=[Optional(), NumberRange(min=0, max=100)],
        render_kw={
            'placeholder': 'e.g. 10%',
            'aria-label': 'Extra percentage on money you owe',
            'data-tooltip': 'Extra percentage you pay on a bank loan or borrowing, like 10% (you can skip this if you don’t know).'
        }
    )
    auto_email = BooleanField(
        'Send Me My Score by Email',
        default=False,
        render_kw={
            'aria-label': 'Send score by email',
            'data-tooltip': 'Check this to get your financial health score sent to your email.'
        }
    )
    record_id = SelectField(
        'Select Record to Edit',
        choices=[('', 'Create New Record')],
        validators=[Optional()],
        render_kw={
            'aria-label': 'Select a previous record',
            'data-tooltip': 'Choose a previous form you filled to edit it, or select "Create New Record" for a new one.'
        }
    )
    submit = SubmitField(
        'Submit',
        render_kw={
            'aria-label': 'Submit your financial information'
        }
    )    
class NetWorthForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    assets = FloatField('Total Assets (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦500,000', 'aria-label': 'Total Assets', 'data-tooltip': 'Enter the total value of your assets.'})
    liabilities = FloatField('Total Liabilities (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦200,000', 'aria-label': 'Total Liabilities', 'data-tooltip': 'Enter the total value of your liabilities.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Get My Net Worth', render_kw={'aria-label': 'Submit Net Worth Form'})
    
class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    q1 = SelectField('Track Income/Expenses', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Track Income/Expenses', 'data-tooltip': 'Do you track your income and expenses?'})
    q2 = SelectField('Save vs Spend', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Save vs Spend', 'data-tooltip': 'Do you save a portion of your income?'})
    q3 = SelectField('Financial Risks', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Financial Risks', 'data-tooltip': 'Are you comfortable with financial risks?'})
    q4 = SelectField('Emergency Fund', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Emergency Fund', 'data-tooltip': 'Do you have an emergency fund?'})
    q5 = SelectField('Review Goals', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Review Goals', 'data-tooltip': 'Do you regularly review your financial goals?'})
    auto_email = BooleanField('Send Email Notification', default=False, render_kw={'aria-label': 'Send Email Notification', 'data-tooltip': 'Check to receive email notifications.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Submit Quiz', render_kw={'aria-label': 'Submit Quiz Form'})
    
class EmergencyFundForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    monthly_expenses = FloatField('Monthly Essential Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦50,000', 'aria-label': 'Monthly Essential Expenses', 'data-tooltip': 'Enter your monthly essential expenses.'})
    auto_email = BooleanField('Send Email Notification', default=False, render_kw={'aria-label': 'Send Email Notification', 'data-tooltip': 'Check to receive email notifications.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Calculate Emergency Fund', render_kw={'aria-label': 'Submit Emergency Fund Form'})

class BudgetForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email(), EqualTo('email', message='Emails must match')], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Confirm Email', 'data-tooltip': 'Re-enter your email to confirm.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    income = FloatField('Total Monthly Income (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦150,000', 'aria-label': 'Total Monthly Income', 'data-tooltip': 'Enter your total monthly income.'})
    housing = FloatField('Housing Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦50,000', 'aria-label': 'Housing Expenses', 'data-tooltip': 'Enter your monthly housing expenses.'})
    food = FloatField('Food Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦30,000', 'aria-label': 'Food Expenses', 'data-tooltip': 'Enter your monthly food expenses.'})
    transport = FloatField('Transport Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦20,000', 'aria-label': 'Transport Expenses', 'data-tooltip': 'Enter your monthly transport expenses.'})
    other = FloatField('Other Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦10,000', 'aria-label': 'Other Expenses', 'data-tooltip': 'Enter other monthly expenses.'})
    auto_email = BooleanField('Send Email Notification', default=False, render_kw={'aria-label': 'Send Email Notification', 'data-tooltip': 'Check to receive email notifications.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Plan My Budget', render_kw={'aria-label': 'Submit Budget Form'})

class ExpenseForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    amount = FloatField('Amount (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦5,000', 'aria-label': 'Amount', 'data-tooltip': 'Enter the transaction amount.'})
    description = TextAreaField('Description', validators=[DataRequired()], render_kw={'placeholder': 'e.g. Grocery shopping', 'aria-label': 'Description', 'data-tooltip': 'Describe the transaction.'})
    category = SelectField('Category', choices=[
        ('Food and Groceries', 'Food and Groceries'),
        ('Transport', 'Transport'),
        ('Housing', 'Housing'),
        ('Utilities', 'Utilities'),
        ('Entertainment', 'Entertainment'),
        ('Other', 'Other')
    ], validators=[DataRequired()], render_kw={'aria-label': 'Category', 'data-tooltip': 'Select the transaction category.'})
    transaction_type = SelectField('Transaction Type', choices=[('Income', 'Income'), ('Expense', 'Expense')], validators=[DataRequired()], render_kw={'aria-label': 'Transaction Type', 'data-tooltip': 'Select if this is income or expense.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    auto_email = BooleanField('Send Email Notification', default=False, render_kw={'aria-label': 'Send Email Notification', 'data-tooltip': 'Check to receive email notifications.'})
    submit = SubmitField('Add Transaction', render_kw={'aria-label': 'Submit Expense Form'})

class BillForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    description = TextAreaField('Description', validators=[DataRequired()], render_kw={'placeholder': 'e.g. Electricity bill', 'aria-label': 'Description', 'data-tooltip': 'Describe the bill.'})
    amount = FloatField('Amount (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦10,000', 'aria-label': 'Amount', 'data-tooltip': 'Enter the bill amount.'})
    due_date = StringField('Due Date', validators=[DataRequired()], render_kw={'placeholder': 'e.g. 2025-06-01', 'aria-label': 'Due Date', 'data-tooltip': 'Enter the bill due date (YYYY-MM-DD).'})
    category = SelectField('Category', choices=[
        ('Utilities', 'Utilities'),
        ('Housing', 'Housing'),
        ('Transport', 'Transport'),
        ('Food', 'Food'),
        ('Other', 'Other')
    ], validators=[DataRequired()], render_kw={'aria-label': 'Category', 'data-tooltip': 'Select the bill category.'})
    recurrence = SelectField('Recurrence', choices=[
        ('None', 'None'),
        ('Daily', 'Daily'),
        ('Weekly', 'Weekly'),
        ('Monthly', 'Monthly'),
        ('Yearly', 'Yearly')
    ], validators=[DataRequired()], render_kw={'aria-label': 'Recurrence', 'data-tooltip': 'Select if the bill recurs.'})
    send_email = BooleanField('Send Email Notification', default=False, render_kw={'aria-label': 'Send Email Notification', 'data-tooltip': 'Check to receive email reminders.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Add Bill', render_kw={'aria-label': 'Submit Bill Form'})

# Celery tasks for email sending
@celery.task(bind=True, max_retries=3)
def send_email_async(self, subject, recipients, html, language='English'):
    if not app.config['MAIL_ENABLED']:
        logger.warning("Email functionality is disabled. Skipping email send.")
        return
    try:
        msg = Message(subject, sender='ficore.ai.africa@gmail.com', recipients=recipients)
        msg.html = html
        with app.app_context():
            mail.send(msg)
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error: {e}")
        self.retry(countdown=60)
    except SMTPException as e:
        logger.error(f"SMTP error: {e}")
        self.retry(countdown=60)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        with app.app_context():
            flash(get_translation('Failed to send email notification', language), 'warning')

@celery.task
def send_bill_reminder_email(bill_json):
    if not app.config['MAIL_ENABLED']:
        logger.warning("Email functionality is disabled. Skipping bill reminder.")
        return
    bill = json.loads(bill_json)
    language = bill.get('Language', 'English')
    try:
        html = render_template(
            'email_templates/bill_reminder_email.html',
            user_name=bill.get('FirstName', ''),
            description=bill.get('Description', ''),
            amount=bill.get('Amount', 0),
            due_date=bill.get('DueDate', ''),
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
            translations=translations.get(language, translations['English'])
        )
        send_email_async.delay(
            get_translation('Bill Reminder Subject', language).format(description=bill.get('Description', '')),
            [bill.get('Email', '')],
            html,
            language
        )
    except Exception as e:
        logger.error(f"Error sending bill reminder: {e}")

@celery.task
def check_reminders():
    try:
        sheet = sheets['BillReminders']
        reminders = sheet.get_all_records()
        now = datetime.now()
        for reminder in reminders:
            if reminder.get('Status') != 'Pending':
                continue
            reminder_date = parse(reminder.get('ReminderDate', '1970-01-01'))
            if reminder_date <= now:
                bill = get_record_by_id(reminder.get('BillTimestamp'), 'BillPlanner')
                if bill and bill.get('Status') == 'Pending':
                    send_reminder_email.delay(json.dumps(bill))
                reminder['Status'] = 'Sent'
                update_or_append_user_data(reminder, 'BillReminders')
    except Exception as e:
        logger.error(f"Error checking bill reminders: {e}")

# Schedule bill reminder using Google Sheets with date validation
def schedule_reminder(bill):
    language = bill.get('Language', 'English')
    try:
        due_date_str = bill.get('DueDate', '')
        try:
            due_date = parse(due_date_str)
            due_date_str = due_date.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            logger.error(f"Invalid due date format for bill: {due_date_str}")
            flash(get_translation('Invalid due date format in bill', language), 'error')
            return
        reminder_date = due_date - timedelta(days=1)
        if reminder_date > datetime.now():
            reminder_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'BillTimestamp': bill.get('Timestamp'),
                'Email': bill.get('Email'),
                'ReminderDate': reminder_date.strftime('%Y-%m-%d %H:%M:%S'),
                'Status': 'Pending'
            }
            update_or_append_user_data(reminder_data, 'BillReminders')
        else:
            flash(get_translation('Reminder date is in the past', language), 'warning')
    except Exception as e:
        logger.error(f"Error scheduling bill reminder: {e}")
        flash(get_translation('Failed to schedule bill reminder due to server error', language), 'error')

# Calculate health score
def calculate_health_score(income_revenue, expenses_costs, debt_loan, debt_interest_rate):
    """
    Calculate financial health score based on user inputs.
    Args:
        income_revenue (float): Money received monthly (e.g., salary, sales).
        expenses_costs (float): Money spent monthly (e.g., rent, food).
        debt_loan (float): Total money owed (e.g., borrowings, loans).
        debt_interest_rate (float): Annual interest rate on debt (%).
    Returns:
        float: Health score between 0 and 100.
    """
    if not income_revenue or income_revenue <= 0:
        return 0
    
    score = 100
    expense_ratio = expenses_costs / income_revenue
    debt_ratio = debt_loan / income_revenue
    
    # Deduct based on expense ratio (up to 40 points)
    if expense_ratio > 1:
        score -= 40
    else:
        score -= 40 * expense_ratio
    
    # Deduct based on debt ratio (up to 30 points)
    if debt_ratio > 1:
        score -= 30
    else:
        score -= 30 * debt_ratio
    
    # Deduct based on interest rate (up to 20 points)
    if debt_interest_rate:
        score -= min(0.5 * debt_interest_rate, 20)
    
    return max(0, round(score, 2))
# Get score description
def get_score_description(score, language='English'):
    if score >= 80:
        return get_translation('Strong Financial Health', language)
    elif score >= 60:
        return get_translation('Stable Finances', language)
    elif score >= 40:
        return get_translation('Financial Strain', language)
    else:
        return get_translation('Urgent Attention Needed', language)

# Assign rank for health score
@cache.memoize(timeout=300)
def assign_rank(score):
    try:
        sheet = sheets['HealthScore']
        all_scores = [parse_number(row.get('Score', 0)) for row in sheet.get_all_records() if row.get('Score') is not None]
        all_scores.append(score)
        sorted_scores = sorted(all_scores, reverse=True)
        rank = sorted_scores.index(score) + 1
        total_users = len(all_scores)
        return rank, total_users
    except Exception as e:
        logger.error(f"Error assigning rank: {e}")
        return 1, 1

# Assign badges for health score
def assign_badges(score, debt, income, language='English'):
    badges = []
    try:
        if score >= 60:
            badges.append(get_translation('Financial Stability Achieved!', language))
        if debt == 0:
            badges.append(get_translation('Debt Slayer!', language))
        if income > 0:
            badges.append(get_translation('First Health Score Completed!', language))
        if score >= 80:
            badges.append(get_translation('High Value Badge', language))
        elif score >= 60:
            badges.append(get_translation('Positive Value Badge', language))
    except Exception as e:
        logger.error(f"Error assigning badges: {e}")
    return badges

# Routes
@app.route('/', methods=['GET'])
def root():
    return redirect(url_for('index'))

@app.route('/index')
def index():
    language = session.get('language', 'English')
    return render_template(
        'index.html',
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/change_language', methods=['POST'])
def change_language():
    language = request.form.get('language', 'English')
    if language in ['English', 'Hausa', 'Yoruba', 'Igbo']:
        session['language'] = language
        flash(get_translation('Language changed successfully', language), 'success')
    else:
        flash(get_translation('Invalid language selection', session.get('language', 'English')), 'error')
    return redirect(request.referrer or url_for('index'))

@app.route('/health_score_form', methods=['GET', 'POST'])
def health_score_form():
    language = session.get('language', 'English')
    form = HealthScoreForm()
    
    # Populate record_id choices for existing records
    user_email = session.get('user_email', '')
    if user_email:
        user_data = get_user_data_by_email('HealthScoreSheet', user_email)
        form.record_id.choices = [('', 'Create New Record')] + [(row['Timestamp'], row['Timestamp']) for row in user_data]
    
    if request.method == 'GET':
        # Pre-fill form for logged-in user or editing a record
        if user_email:
            form.email.data = user_email
            form.email.render_kw['readonly'] = True
            form.confirm_email.data = user_email
            form.confirm_email.render_kw['readonly'] = True
        record_id = request.args.get('record_id')
        if record_id:
            user_data = get_user_data_by_email('HealthScoreSheet', user_email)
            record = next((row for row in user_data if row['Timestamp'] == record_id), None)
            if record:
                form.first_name.data = record['FirstName']
                form.last_name.data = record['LastName']
                form.phone_number.data = record['PhoneNumber']
                form.language.data = record['Language']
                form.business_name.data = record['BusinessName']
                form.user_type.data = record['UserType']
                form.income_revenue.data = record['IncomeRevenue']
                form.expenses_costs.data = record['ExpensesCosts']
                form.debt_loan.data = record['DebtLoan']
                form.debt_interest_rate.data = record['DebtInterestRate']
                form.auto_email.data = record['AutoEmail'].lower() == 'true'
                form.record_id.data = record_id
    
    if form.validate_on_submit():
        # Store session data
        session['language'] = form.language.data
        session['user_email'] = form.email.data
        session['first_name'] = form.first_name.data
        session['session_id'] = session.get('session_id', str(uuid.uuid4()))
        
        # Store authentication data
        store_authentication_data(form.email.data, session['session_id'])
        
        # Parse numeric inputs
        income = parse_number(form.income_revenue.data)
        expenses = parse_number(form.expenses_costs.data)
        debt = parse_number(form.debt_loan.data)
        interest_rate = parse_number(form.debt_interest_rate.data) or 0
        
        # Calculate health score
        health_score = calculate_health_score(income, expenses, debt, interest_rate)
        score_description = get_score_description(health_score, form.language.data)
        rank, total_users = assign_rank(health_score, 'HealthScoreSheet')
        badges = assign_badges(health_score, debt, True)
        
        # Prepare user data for Google Sheets
        user_data = {
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'BusinessName': form.business_name.data,
            'IncomeRevenue': income,
            'ExpensesCosts': expenses,
            'DebtLoan': debt,
            'DebtInterestRate': interest_rate,
            'AutoEmail': str(form.auto_email.data),
            'PhoneNumber': form.phone_number.data or '',
            'FirstName': form.first_name.data,
            'LastName': form.last_name.data or '',
            'UserType': form.user_type.data,
            'Email': form.email.data,
            'Badges': ','.join(badges),
            'Language': form.language.data,
            'Score': health_score
        }
        
        # Store data in Google Sheets
        try:
            update_or_append_user_data('HealthScoreSheet', user_data, form.record_id.data)
        except Exception as e:
            flash(get_translation('Error saving data. Please try again.', form.language.data), 'danger')
            return render_template(
                'health_score_form.html',
                form=form,
                translations=translations.get(form.language.data, translations['English']),
                language=form.language.data
            )
        
        # Send email if enabled
        if form.auto_email.data and app.config.get('MAIL_ENABLED', False):
            try:
                send_email_async(
                    form.email.data,
                    get_translation('Your Financial Health Score', form.language.data),
                    'email_templates/health_score_email.html',
                    first_name=form.first_name.data,
                    health_score=health_score,
                    score_description=score_description,
                    rank=rank,
                    total_users=total_users,
                    course=get_courses(form.language.data)[0],
                    consultancy_form_url='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
                )
            except Exception as e:
                flash(get_translation('Error sending email. Your score is still saved.', form.language.data), 'warning')
        
        # Generate charts
        try:
            chart_html, comparison_chart_html = generate_health_score_charts(
                income=income,
                debt=debt,
                health_score=health_score,
                average_score=get_average_health_score('HealthScoreSheet'),
                language=form.language.data
            )
        except Exception as e:
            flash(get_translation('Error generating charts. Your score is still available.', form.language.data), 'warning')
            chart_html = comparison_chart_html = ''
        
        # Store dashboard data in session
        session['health_score_data'] = {
            'user_data': user_data,
            'chart_html': chart_html,
            'comparison_chart_html': comparison_chart_html,
            'score_description': score_description,
            'rank': rank,
            'total_users': total_users,
            'badges': badges
        }
        
        return redirect(url_for('health_score_dashboard'))
    
    return render_template(
        'health_score_form.html',
        form=form,
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/health_score_dashboard')
def health_score_dashboard():
    language = session.get('language', 'English')
    data = session.get('health_score_data', {})
    
    # Validate dashboard access
    if not data.get('user_data', {}).get('FirstName'):
        flash(get_translation('Invalid dashboard access. Please complete the form.', language), 'danger')
        return redirect(url_for('health_score_form'))
    
    return render_template(
        'health_score_dashboard.html',
        tool='Financial Health Score',
        user_data=data.get('user_data', {}),
        chart_html=data.get('chart_html', ''),
        comparison_chart_html=data.get('comparison_chart_html', ''),
        score_description=data.get('score_description', ''),
        rank=data.get('rank', '1'),
        total_users=data.get('total_users', '1'),
        badges=data.get('badges', []),
        tips=get_tips(language),
        courses=get_courses(language),
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/net_worth_form', methods=['GET', 'POST'])
def net_worth_form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = None
    record_choices = [('', get_translation('Create New Record', language))]
    user_records = get_user_data_by_email(email, 'NetWorth') if email else []
    if user_records:
        for record in user_records:
            timestamp = record.get('Timestamp', 'Unknown')
            record_choices.append((timestamp, f"Record from {timestamp}"))
    selected_record_id = request.args.get('record_id', '') if request.method == 'GET' else None
    if selected_record_id and email:
        for record in user_records:
            if record.get('Timestamp') == selected_record_id:
                form_data = record
                break

    form = NetWorthForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        assets=form_data.get('Assets') if form_data else None,
        liabilities=form_data.get('Liabilities') if form_data else None,
        record_id=selected_record_id
    )
    form.record_id.choices = record_choices
    if email:
        form.email.render_kw = {'readonly': True}

    if request.method == 'POST':
        if form.validate_on_submit():
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session['first_name'] = form.first_name.data
            session.permanent = True

            store_authentication_data({
                'first_name': form.first_name.data,
                'email': form.email.data,
                'language': form.language.data
            })

            assets = parse_number(form.assets.data)
            liabilities = parse_number(form.liabilities.data)
            net_worth = assets - liabilities

            rank_percentile = assign_net_worth_rank(net_worth)
            badges = assign_net_worth_badges(net_worth, language)
            advice = get_net_worth_advice(net_worth, language)

            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'Language': form.language.data,
                'Assets': assets,
                'Liabilities': liabilities,
                'NetWorth': net_worth
            }
            if form.record_id.data:
                user_data['Timestamp'] = form.record_id.data
            update_or_append_user_data(user_data, 'NetWorth')

            try:
                chart_html, comparison_chart_html = generate_net_worth_charts(json.dumps(user_data), language)
            except Exception as e:
                chart_html, comparison_chart_html = '', ''
                flash(get_translation('Error generating charts', language), 'danger')

            return redirect(url_for('net_worth_dashboard',
                                    user_data=json.dumps(user_data),
                                    chart_html=chart_html,
                                    comparison_chart_html=comparison_chart_html,
                                    rank_percentile=rank_percentile,
                                    badges=json.dumps(badges),
                                    advice=advice))
        else:
            flash(get_translation('Form validation failed. Please check your inputs.', language), 'danger')

    return render_template(
        'net_worth_form.html',
        form=form,
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/net_worth_dashboard')
def net_worth_dashboard():
    language = session.get('language', 'English')
    user_data = json.loads(request.args.get('user_data', '{}')) if request.args.get('user_data') else {}
    chart_html = request.args.get('chart_html', '')
    comparison_chart_html = request.args.get('comparison_chart_html', '')
    rank_percentile = request.args.get('rank_percentile', '50.0')
    badges = json.loads(request.args.get('badges', '[]')) if request.args.get('badges') else []
    advice = request.args.get('advice', get_translation('No advice available', language))

    if not user_data.get('FirstName') or not user_data.get('NetWorth'):
        flash(get_translation('Invalid dashboard access', language), 'danger')
        return redirect(url_for('net_worth_form'))

    return render_template(
        'net_worth_dashboard.html',
        tool='Net Worth Calculator',
        user_data=user_data,
        chart_html=chart_html,
        comparison_chart_html=comparison_chart_html,
        rank_percentile=rank_percentile,
        badges=badges,
        advice=advice,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/quiz_form', methods=['GET', 'POST'])
def quiz_form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = None
    record_choices = [('', get_translation('Create New Record', language))]
    user_records = get_user_data_by_email(email, 'Quiz') if email else []
    if user_records:
        for record in user_records:
            timestamp = record.get('Timestamp', 'Unknown')
            record_choices.append((timestamp, f"Record from {timestamp}"))
    selected_record_id = request.args.get('record_id', '') if request.method == 'GET' else None
    if selected_record_id and email:
        for record in user_records:
            if record.get('Timestamp') == selected_record_id:
                form_data = record
                break
    form = QuizForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        q1=form_data.get('Q1') if form_data else None,
        q2=form_data.get('Q2') if form_data else None,
        q3=form_data.get('Q3') if form_data else None,
        q4=form_data.get('Q4') if form_data else None,
        q5=form_data.get('Q5') if form_data else None,
        record_id=selected_record_id
    )
    form.record_id.choices = record_choices
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session['first_name'] = form.first_name.data
            session.permanent = True

            store_authentication_data({
                'first_name': form.first_name.data,
                'email': form.email.data,
                'language': form.language.data
            })

            score = sum(1 for q in ['q1', 'q2', 'q3', 'q4', 'q5'] if getattr(form, q).data == 'Yes')
            personality = 'Conservative' if score <= 2 else 'Balanced' if score <= 4 else 'Aggressive'
            badges = assign_quiz_badges(score, language)
            advice = get_quiz_advice(score, personality, language)
            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'Language': form.language.data,
                'Q1': form.q1.data,
                'Q2': form.q2.data,
                'Q3': form.q3.data,
                'Q4': form.q4.data,
                'Q5': form.q5.data,
                'QuizScore': score,
                'Personality': personality
            }
            if form.record_id.data:
                user_data['Timestamp'] = form.record_id.data
            update_or_append_user_data(user_data, 'Quiz')
            if form.auto_email.data:
                html = render_template(
                    'email_templates/quiz_email.html',
                    user_name=form.first_name.data,
                    score=score,
                    personality=personality,
                    badges=badges,
                    advice=advice,
                    FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                    WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                    CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
                    translations=translations.get(language, translations['English'])
                )
                send_email_async.delay(
                    get_translation('Quiz Report Subject', language).format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(get_translation('Email scheduled to be sent', language), 'success')
            return redirect(url_for('quiz_dashboard', user_data=json.dumps(user_data), score=score, personality=personality, badges=json.dumps(badges), advice=advice))
    return render_template(
        'quiz_form.html',
        form=form,
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/quiz_dashboard')
def quiz_dashboard():
    language = session.get('language', 'English')
    user_data = json.loads(request.args.get('user_data', '{}'))
    score = request.args.get('score', '0')
    personality = request.args.get('personality', '')
    badges = json.loads(request.args.get('badges', '[]'))
    advice = request.args.get('advice', '')
    return render_template(
        'quiz_dashboard.html',
        tool='Financial Quiz',
        user_data=user_data,
        score=score,
        personality=personality,
        badges=badges,
        advice=advice,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/emergency_fund_form', methods=['GET', 'POST'])
def emergency_fund_form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = None
    record_choices = [('', get_translation('Create New Record', language))]
    user_records = get_user_data_by_email(email, 'EmergencyFund') if email else []
    if user_records:
        for record in user_records:
            timestamp = record.get('Timestamp', 'Unknown')
            record_choices.append((timestamp, f"Record from {timestamp}"))
    selected_record_id = request.args.get('record_id', '') if request.method == 'GET' else None
    if selected_record_id and email:
        for record in user_records:
            if record.get('Timestamp') == selected_record_id:
                form_data = record
                break
    form = EmergencyFundForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        monthly_expenses=form_data.get('MonthlyExpenses') if form_data else None,
        record_id=selected_record_id
    )
    form.record_id.choices = record_choices
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session['first_name'] = form.first_name.data
            session.permanent = True

            store_authentication_data({
                'first_name': form.first_name.data,
                'email': form.email.data,
                'language': form.language.data
            })

            monthly_expenses = parse_number(form.monthly_expenses.data)
            recommended_fund = monthly_expenses * 3
            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'Language': form.language.data,
                'MonthlyExpenses': monthly_expenses,
                'RecommendedFund': recommended_fund
            }
            if form.record_id.data:
                user_data['Timestamp'] = form.record_id.data
            update_or_append_user_data(user_data, 'EmergencyFund')
            if form.auto_email.data:
                html = render_template(
                    'email_templates/emergency_fund_email.html',
                    user_name=form.first_name.data,
                    monthly_expenses=monthly_expenses,
                    recommended_fund=recommended_fund,
                    FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                    WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                    CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
                    translations=translations.get(language, translations['English'])
                )
                send_email_async.delay(
                    get_translation('Emergency Fund Report Subject', language).format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(get_translation('Email scheduled to be sent', language), 'success')
            return redirect(url_for('emergency_fund_dashboard', user_data=json.dumps(user_data)))
        else:
            flash(get_translation('Form validation failed. Please check your inputs.', language), 'danger')
    
    return render_template(
        'emergency_fund_form.html',
        form=form,
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/emergency_fund_dashboard')
def emergency_fund_dashboard():
    language = session.get('language', 'English')
    user_data = json.loads(request.args.get('user_data', '{}'))
    if not user_data.get('FirstName') or not user_data.get('RecommendedFund'):
        flash(get_translation('Invalid dashboard access', language), 'danger')
        return redirect(url_for('emergency_fund_form'))
    return render_template(
        'emergency_fund_dashboard.html',
        tool='Emergency Fund Calculator',
        user_data=user_data,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/budget_form', methods=['GET', 'POST'])
def budget_form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = None
    record_choices = [('', get_translation('Create New Record', language))]
    user_records = get_user_data_by_email(email, 'Budget') if email else []
    if user_records:
        for record in user_records:
            timestamp = record.get('Timestamp', 'Unknown')
            record_choices.append((timestamp, f"Record from {timestamp}"))
    selected_record_id = request.args.get('record_id', '') if request.method == 'GET' else None
    if selected_record_id and email:
        for record in user_records:
            if record.get('Timestamp') == selected_record_id:
                form_data = record
                break
    form = BudgetForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        income=form_data.get('MonthlyIncome') if form_data else None,
        housing=form_data.get('HousingExpenses') if form_data else None,
        food=form_data.get('FoodExpenses') if form_data else None,
        transport=form_data.get('TransportExpenses') if form_data else None,
        other=form_data.get('OtherExpenses') if form_data else None,
        record_id=selected_record_id
    )
    form.record_id.choices = record_choices
    if email:
        form.email.render_kw = {'readonly': True}
        form.confirm_email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session['first_name'] = form.first_name.data
            session.permanent = True

            store_authentication_data({
                'first_name': form.first_name.data,
                'email': form.email.data,
                'language': form.language.data
            })

            income = parse_number(form.income.data)
            housing = parse_number(form.housing.data)
            food = parse_number(form.food.data)
            transport = parse_number(form.transport.data)
            other = parse_number(form.other.data)
            total_expenses = housing + food + transport + other
            savings = max(0, income * 0.1)
            surplus_deficit = income - total_expenses - savings

            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'AutoEmail': str(form.auto_email.data),
                'Language': form.language.data,
                'MonthlyIncome': income,
                'HousingExpenses': housing,
                'FoodExpenses': food,
                'TransportExpenses': transport,
                'OtherExpenses': other,
                'TotalExpenses': total_expenses,
                'Savings': savings,
                'SurplusDeficit': surplus_deficit
            }
            if form.record_id.data:
                user_data['Timestamp'] = form.record_id.data
            update_or_append_user_data(user_data, 'Budget')

if form.auto_email.data:
    course = {
        'url': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru',
        'title': 'Ficore Africa Financial Tips'
    }
    html = render_template(
        'email_templates/budget_email.html',
        user_name=form.first_name.data,
        income=income,
        housing=housing,
        food=food,
        transport=transport,
        other=other,
        total_expenses=total_expenses,
        savings=savings,
        surplus_deficit=surplus_deficit,
        course_url=course['url'],
        course_title=course['title'],
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
        translations=translations.get(language, translations['English'])
    )
                send_email_async.delay(
                    get_translation('Budget Report Subject', language).format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(get_translation('Email scheduled to be sent', language), 'success')

            try:
                chart_html = generate_budget_charts(json.dumps(user_data), language)
            except Exception as e:
                chart_html = ''
                flash(get_translation('Error generating charts', language), 'danger')

            return redirect(url_for('budget_dashboard', user_data=json.dumps(user_data), chart_html=chart_html))
        else:
            flash(get_translation('Form validation failed. Please check your inputs.', language), 'danger')

    return render_template(
        'budget_form.html',
        form=form,
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/budget_dashboard')
def budget_dashboard():
    language = session.get('language', 'English')
    user_data = json.loads(request.args.get('user_data', '{}'))
    chart_html = request.args.get('chart_html', '')
    if not user_data.get('FirstName') or not user_data.get('MonthlyIncome'):
        flash(get_translation('Invalid dashboard access', language), 'danger')
        return redirect(url_for('budget_form'))
    return render_template(
        'budget_dashboard.html',
        tool='Budget Planner',
        user_data=user_data,
        chart_html=chart_html,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/expense_tracker_form', methods=['GET', 'POST'])
def expense_tracker_form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = None
    record_choices = [('', get_translation('Create New Record', language))]
    user_records = get_user_data_by_email(email, 'ExpenseTracker') if email else []
    if user_records:
        for record in user_records:
            timestamp = record.get('Timestamp', 'Unknown')
            record_choices.append((timestamp, f"Record from {timestamp}"))
    selected_record_id = request.args.get('record_id', '') if request.method == 'GET' else None
    if selected_record_id and email:
        for record in user_records:
            if record.get('Timestamp') == selected_record_id:
                form_data = record
                break
    form = ExpenseForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        amount=form_data.get('Amount') if form_data else None,
        description=form_data.get('Description') if form_data else None,
        category=form_data.get('Category') if form_data else None,
        transaction_type=form_data.get('TransactionType') if form_data else None,
        record_id=selected_record_id
    )
    form.record_id.choices = record_choices
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session['first_name'] = form.first_name.data
            session.permanent = True

            store_authentication_data({
                'first_name': form.first_name.data,
                'email': form.email.data,
                'language': form.language.data
            })

            amount = parse_number(form.amount.data)
            user_data = {
                'ID': str(uuid.uuid4()),
                'UserEmail': form.email.data,
                'Amount': amount,
                'Category': form.category.data,
                'Date': datetime.now().strftime('%Y-%m-%d'),
                'Description': form.description.data,
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'TransactionType': form.transaction_type.data,
                'RunningBalance': 0
            }
            if form.record_id.data:
                user_data['Timestamp'] = form.record_id.data
            update_or_append_user_data(user_data, 'ExpenseTracker')
            running_balance = calculate_running_balance(form.email.data)

            if form.auto_email.data:
                html = render_template(
                    'email_templates/expense_email.html',
                    user_name=form.first_name.data,
                    amount=amount,
                    category=form.category.data,
                    description=form.description.data,
                    transaction_type=form.transaction_type.data,
                    running_balance=running_balance,
                    FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                    WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                    CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
                    translations=translations.get(language, translations['English'])
                )
                send_email_async.delay(
                    get_translation('Transaction Report Subject', language).format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(get_translation('Email scheduled to be sent', language), 'success')

            try:
                chart_html = generate_expense_charts(form.email.data, language)
            except Exception as e:
                chart_html = ''
                flash(get_translation('Error generating charts', language), 'danger')

            return redirect(url_for('expense_tracker_dashboard', chart_html=chart_html, running_balance=running_balance))
        else:
            flash(get_translation('Form validation failed. Please check your inputs.', language), 'danger')

    return render_template(
        'expense_tracker_form.html',
        form=form,
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/expense_tracker_dashboard')
def expense_dashboard():
    language = session.get('language', 'English')
    email = session.get('user_email')
    chart_html = request.args.get('chart_html', '')
    running_balance = request.args.get('running_balance', '0')
    records = get_user_data_by_email(email, 'ExpenseTracker') if email else []
    return render_template(
        'expense_tracker_dashboard.html',
        tool='Expense Tracker',
        records=records,
        chart_html=chart_html,
        running_balance=running_balance,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/bill_planner_form', methods=['GET', 'POST'])
def bill_planner_form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = None
    record_choices = [('', get_translation('Create New Record', language))]
    user_records = get_user_data_by_email(email, 'BillPlanner') if email else []
    if user_records:
        for record in user_records:
            timestamp = record.get('Timestamp', 'Unknown')
            record_choices.append((timestamp, f"Record from {timestamp}"))
    selected_record_id = request.args.get('record_id', '') if request.method == 'GET' else None
    if selected_record_id and email:
        for record in user_records:
            if record.get('Timestamp') == selected_record_id:
                form_data = record
                break
    form = BillForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        description=form_data.get('Description') if form_data else None,
        amount=form_data.get('Amount') if form_data else None,
        due_date=form_data.get('DueDate') if form_data else None,
        category=form_data.get('Category') if form_data else None,
        recurrence=form_data.get('Recurrence') if form_data else None,
        send_email=form_data.get('SendEmail', False) if form_data else False,
        record_id=selected_record_id
    )
    form.record_id.choices = record_choices
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session['first_name'] = form.first_name.data
            session.permanent = True

            store_authentication_data({
                'first_name': form.first_name.data,
                'email': form.email.data,
                'language': form.language.data
            })

            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'Language': form.language.data,
                'Description': form.description.data,
                'Amount': parse_number(form.amount.data),
                'DueDate': form.due_date.data,
                'Category': form.category.data,
                'Recurrence': form.recurrence.data,
                'Status': 'Pending',
                'SendEmail': str(form.send_email.data)
            }
            if form.record_id.data:
                user_data['Timestamp'] = form.record_id.data
            update_or_append_user_data(user_data, 'BillPlanner')

            if form.send_email.data:
                schedule_bill_reminder(user_data)
                flash(get_translation('Bill reminder scheduled', language), 'success')

            return redirect(url_for('bill_planner_dashboard'))
        else:
            flash(get_translation('Form validation failed. Please check your inputs.', language), 'danger')

    return render_template(
        'bill_planner_form.html',
        form=form,
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/bill_planner_dashboard')
def bill_planner_dashboard():
    language = session.get('language', 'English')
    email = session.get('user_email')
    records = get_user_data_by_email(email, 'BillPlanner') if email else []
    return render_template(
        'bill_planner_dashboard.html',
        tool='Bill Planner',
        records=records,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=translations.get(language, translations['English']),
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/mark_bill_paid/<bill_id>', methods=['POST'])
def mark_bill_paid(bill_id):
    language = session.get('language', 'English')
    bill = get_record_by_id(bill_id, 'BillPlanner')
    if not bill:
        flash(get_translation('Bill not found', language), 'danger')
        return redirect(url_for('bill_planner_dashboard'))
    bill['Status'] = 'Paid'
    update_or_append_user_data(bill, 'BillPlanner')
    flash(get_translation('Bill marked as paid', language), 'success')
    return redirect(url_for('bill_planner_dashboard'))

# Error handlers
@app.errorhandler(404)
def page_not_found(e):
    language = session.get('language', 'English')
    return render_template(
        'error.html',
        error_code=404,
        error_message=get_translation('Page not found', language),
        translations=translations.get(language, translations['English']),
        language=language
    ), 404

@app.errorhandler(500)
def internal_server_error(e):
    language = session.get('language', 'English')
    return render_template(
        'error.html',
        error_code=500,
        error_message=get_translation('Internal server error', language),
        translations=translations.get(language, translations['English']),
        language=language
    ), 500

# Redis connection cleanup
def cleanup_redis():
    try:
        redis_client = redis.Redis.from_url(celery_broker_url)
        redis_client.close()
    except Exception as e:
        logger.error(f"Error closing Redis connection: {e}")

atexit.register(cleanup_redis)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
