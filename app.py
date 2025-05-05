import logging

# Configure logging to output to stdout (required for Render)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
import os
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
logging.basicConfig(filename='app.log', level=logging.ERROR, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
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
app.config['CELERY_BROKER_URL'] = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
app.config['CELERY_RESULT_BACKEND'] = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
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
if not app.config['MAIL_PASSWORD']:
    logger.error("SMTP_PASSWORD environment variable not set")
mail = Mail(app)

# Initialize Google Sheets client with google-auth
scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
try:
    creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if not creds_json:
        logger.error("GOOGLE_CREDENTIALS_JSON environment variable not set")
        abort(500)
    
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(creds)
except json.JSONDecodeError as e:
    logger.error(f"Invalid GOOGLE_CREDENTIALS_JSON format: {e}")
    flash("Server error: Invalid Google Sheets credentials configuration", 'error')
    abort(500)
except Exception as e:
    logger.error(f"Failed to initialize Google Sheets credentials: {e}")
    abort(500)

try:
    spreadsheet = client.open_by_key('13hbiMTMRBHo9MHjWwcugngY_aSiuxII67HCf03MiZ8I')
except Exception as e:
    logger.error(f"Error accessing Google Sheets: {e}")
    abort(500)

# Define worksheet configurations
WORKSHEETS = {
    'HealthScore': {'name': 'HealthScoreSheet', 'headers': ['Timestamp', 'BusinessName', 'IncomeRevenue', 'ExpensesCosts', 'DebtLoan', 'DebtInterestRate', 'AutoEmail', 'PhoneNumber', 'FirstName', 'LastName', 'UserType', 'Email', 'Badges', 'Language', 'Score']},
    'NetWorth': {'name': 'NetWorthSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Assets', 'Liabilities', 'NetWorth']},
    'Quiz': {'name': 'QuizSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'QuizScore', 'Personality']},
    'EmergencyFund': {'name': 'EmergencyFundSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'MonthlyExpenses', 'RecommendedFund']},
    'Budget': {'name': 'BudgetSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'AutoEmail', 'Language', 'MonthlyIncome', 'HousingExpenses', 'FoodExpenses', 'TransportExpenses', 'OtherExpenses', 'TotalExpenses', 'Savings', 'SurplusDeficit']},
    'ExpenseTracker': {'name': 'ExpenseTrackerSheet', 'headers': ['ID', 'UserEmail', 'Amount', 'Category', 'Date', 'Description', 'Timestamp', 'TransactionType', 'RunningBalance']},
    'BillPlanner': {'name': 'BillPlannerSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Description', 'Amount', 'DueDate', 'Category', 'Recurrence', 'Status', 'SendEmail']},
    'BillReminders': {'name': 'BillRemindersSheet', 'headers': ['Timestamp', 'BillTimestamp', 'Email', 'ReminderDate', 'Status']}
}

# Initialize worksheets and ensure headers
sheets = {}
for tool, config in WORKSHEETS.items():
    try:
        sheets[tool] = spreadsheet.worksheet(config['name'])
    except gspread.exceptions.WorksheetNotFound:
        sheets[tool] = spreadsheet.add_worksheet(title=config['name'], rows=100, cols=50)
        sheets[tool].append_row(config['headers'])
    try:
        current_headers = sheets[tool].row_values(1)
        if not current_headers or current_headers != config['headers']:
            sheets[tool].clear()
            sheets[tool].append_row(config['headers'])
    except Exception as e:
        logger.error(f"Error setting headers for {config['name']}: {e}")
        sheets[tool].clear()
        sheets[tool].append_row(config['headers'])

# Utility function to parse numbers with comma support
def parse_number(value):
    try:
        if isinstance(value, str):
            value = value.replace(',', '')
        return float(value)
    except (ValueError, TypeError):
        return 0

# Fetch user data by email with validation
def get_user_data_by_email(email, tool):
    try:
        records = sheets[tool].get_all_records()
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
        records = sheets[tool].get_all_records()
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
        flash(translations.get(language, translations['English'])['Failed to save data due to Google Sheets API limit'], 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])
    except Exception as e:
        logger.error(f"Error updating/appending data to {WORKSHEETS[tool]['name']}: {e}")
        flash(translations.get(language, translations['English'])['Failed to save data due to server error'], 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])

# Calculate running balance for expense tracker (optimized)
def calculate_running_balance(email):
    try:
        records = sheets['ExpenseTracker'].get_all_records()
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
        all_net_worths = [parse_number(row.get('NetWorth', 0)) for row in sheets['NetWorth'].get_all_records() if row.get('NetWorth') and row.get('NetWorth').strip()]
        all_net_worths.append(net_worth)
        rank_percentile = 100 - np.percentile(all_net_worths, np.searchsorted(sorted(all_net_worths, reverse=True), net_worth) / len(all_net_worths) * 100)
        return round(rank_percentile, 1)
    except Exception as e:
        logger.error(f"Error assigning net worth rank: {e}")
        return 50.0

# Get net worth advice
def get_net_worth_advice(net_worth, language='English'):
    trans = translations.get(language, translations['English'])
    if net_worth > 0:
        return trans['Maintain your positive net worth by continuing to manage liabilities and grow assets.']
    elif net_worth == 0:
        return trans['Your net worth is balanced. Consider increasing assets to build wealth.']
    else:
        return trans['Focus on reducing liabilities to improve your net worth.']

# Assign net worth badges
def assign_net_worth_badges(net_worth, language='English'):
    badges = []
    trans = translations.get(language, translations['English'])
    try:
        if net_worth > 0:
            badges.append(trans['Positive Net Worth'])
        if net_worth >= 100000:
            badges.append(trans['Wealth Builder'])
        if net_worth <= -50000:
            badges.append(trans['Debt Recovery'])
    except Exception as e:
        logger.error(f"Error assigning net worth badges: {e}")
    return badges

# Get financial tips
def get_tips(language='English'):
    trans = translations.get(language, translations['English'])
    return [
        trans['Regularly review your assets and liabilities to track progress.'],
        trans['Invest in low-risk assets to grow your wealth steadily.'],
        trans['Create a plan to pay down high-interest debt first.']
    ]

# Get recommended courses
def get_courses(language='English'):
    trans = translations.get(language, translations['English'])
    return [
        {'title': trans['Personal Finance 101'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': trans['Debt Management Basics'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': trans['Investing for Beginners'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'}
    ]

# Get quiz advice
def get_quiz_advice(score, personality, language='English'):
    trans = translations.get(language, translations['English'])
    if score >= 4:
        return trans['Great job! Continue to leverage your {personality} approach to build wealth.'].format(personality=personality.lower())
    elif score >= 2:
        return trans['Good effort! Your {personality} style is solid, but consider tracking expenses more closely.'].format(personality=personality.lower())
    else:
        return trans['Keep learning! Your {personality} approach can improve with regular financial reviews.'].format(personality=personality.lower())

# Assign quiz badges
def assign_quiz_badges(score, language='English'):
    badges = []
    trans = translations.get(language, translations['English'])
    try:
        if score >= 4:
            badges.append(trans['Financial Guru'])
        if score >= 2:
            badges.append(trans['Quiz Achiever'])
        badges.append(trans['Quiz Participant'])
    except Exception as e:
        logger.error(f"Error assigning quiz badges: {e}")
    return badges

# Get average health score with caching
@cache.memoize(timeout=300)
def get_average_health_score():
    try:
        records = sheets['HealthScore'].get_all_records()
        scores = [parse_number(row.get('Score', 0)) for row in records if row.get('Score') and row.get('Score').strip()]
        return np.mean(scores) if scores else 50
    except Exception as e:
        logger.error(f"Error calculating average health score: {e}")
        return 50

# Generate health score charts with caching
@cache.memoize(timeout=300)
def generate_health_score_charts(user_data_json, language='English'):
    user_data = json.loads(user_data_json)
    trans = translations.get(language, translations['English'])
    try:
        income = parse_number(user_data.get('IncomeRevenue', 0))
        debt = parse_number(user_data.get('DebtLoan', 0))
        ratio_message = trans['Your asset-to-liability ratio is healthy.'] if income >= 2 * debt else trans['Your liabilities are high. Consider strategies to reduce debt.']
        bar_fig = go.Figure(data=[
            go.Bar(name=trans['Income (₦)'], x=['Income'], y=[income], marker_color='#2E7D32'),
            go.Bar(name=trans['Debt (₦)'], x=['Debt'], y=[debt], marker_color='#DC3545')
        ])
        bar_fig.update_layout(
            title=trans['Asset-Liability Breakdown'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            annotations=[dict(text=ratio_message, xref="paper", yref="paper", x=0.5, y=1.1, showarrow=False)],
            hovermode='closest'
        )
        chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=True)

        average_score = get_average_health_score()
        bar_fig = go.Figure(data=[
            go.Bar(name=trans['Your Score'], x=['You'], y=[user_data.get('Score', 0)], marker_color='#2E7D32'),
            go.Bar(name=trans['Average Score'], x=['Average'], y=[average_score], marker_color='#0288D1')
        ])
        bar_fig.update_layout(
            title=trans['Comparison to Peers'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            hovermode='closest'
        )
        comparison_chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=True)
        return chart_html, comparison_chart_html
    except Exception as e:
        logger.error(f"Error generating health score charts: {e}")
        return trans['Chart failed to load. Please try again.'], ""

# Generate net worth charts with caching
@cache.memoize(timeout=300)
def generate_net_worth_charts(net_worth_data_json, language='English'):
    net_worth_data = json.loads(net_worth_data_json)
    trans = translations.get(language, translations['English'])
    try:
        labels = [trans['Assets'], trans['Liabilities']]
        values = [parse_number(net_worth_data.get('Assets', 0)), parse_number(net_worth_data.get('Liabilities', 0))]
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545']))])
        pie_fig.update_layout(
            title=trans['Asset-Liability Breakdown'],
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            hovermode='closest'
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=True)

        all_net_worths = [parse_number(row.get('NetWorth', 0)) for row in sheets['NetWorth'].get_all_records() if row.get('NetWorth') and row.get('NetWorth').strip()]
        user_net_worth = parse_number(net_worth_data.get('NetWorth', 0))
        avg_net_worth = np.mean(all_net_worths) if all_net_worths else 0
        bar_fig = go.Figure(data=[
            go.Bar(name=trans['Your Net Worth'], x=['You'], y=[user_net_worth], marker_color='#2E7D32'),
            go.Bar(name=trans['Average Net Worth'], x=['Average'], y=[avg_net_worth], marker_color='#0288D1')
        ])
        bar_fig.update_layout(
            title=trans['Comparison to Peers'],
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
        return trans['Chart failed to load. Please try again.'], ""

# Generate budget charts with caching
@cache.memoize(timeout=300)
def generate_budget_charts(budget_data_json, language='English'):
    budget_data = json.loads(budget_data_json)
    trans = translations.get(language, translations['English'])
    try:
        labels = [
            trans['Housing'],
            trans['Food'],
            trans['Transport'],
            trans['Other'],
            trans['Savings']
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
            title=trans['Budget Breakdown'],
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
        return trans['Chart failed to load. Please try again.']

# Generate expense charts with caching
@cache.memoize(timeout=300)
def generate_expense_charts(email, language='English'):
    trans = translations.get(language, translations['English'])
    try:
        records = sheets['ExpenseTracker'].get_all_records()
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
            return trans['No expense data available.']
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545', '#0288D1', '#FFB300', '#4CAF50', '#9C27B0']))])
        pie_fig.update_layout(
            title=trans['Expense Breakdown by Category'],
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
        return trans['Chart failed to load. Please try again.']

# Form definitions with enhanced UI features
class UserForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    last_name = StringField('Last Name', validators=[Optional()], render_kw={'placeholder': 'e.g. Doe', 'aria-label': 'Last Name', 'data-tooltip': 'Enter your last name (optional).'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email(), EqualTo('email', message='Emails must match')], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Confirm Email', 'data-tooltip': 'Re-enter your email to confirm.'})
    phone = StringField('Phone Number', validators=[Optional()], render_kw={'placeholder': 'e.g. +234123456789', 'aria-label': 'Phone Number', 'data-tooltip': 'Enter your phone number (optional).'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa'), ('Yoruba', 'Yoruba'), ('Igbo', 'Igbo')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    business_name = StringField('Business Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. My Business or Personal', 'aria-label': 'Business Name', 'data-tooltip': 'Enter your business name or "Personal".'})
    user_type = SelectField('User Type', choices=[('Individual', 'Individual'), ('Business', 'Business')], validators=[DataRequired()], render_kw={'aria-label': 'User Type', 'data-tooltip': 'Select if you are an individual or business.'})
    income = FloatField('Monthly Income (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦150,000', 'aria-label': 'Monthly Income', 'data-tooltip': 'Enter your total monthly income.'})
    expenses = FloatField('Monthly Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦100,000', 'aria-label': 'Monthly Expenses', 'data-tooltip': 'Enter your total monthly expenses.'})
    debt = FloatField('Total Debt (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦50,000', 'aria-label': 'Total Debt', 'data-tooltip': 'Enter your total debt amount.'})
    interest_rate = FloatField('Debt Interest Rate (%)', validators=[Optional(), NumberRange(min=0, max=100)], render_kw={'placeholder': 'e.g. 10%', 'aria-label': 'Debt Interest Rate', 'data-tooltip': 'Enter the annual interest rate for your debt (optional).'})
    auto_email = BooleanField('Send Email Notification', default=False, render_kw={'aria-label': 'Send Email Notification', 'data-tooltip': 'Check to receive email notifications.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Check My Financial Health', render_kw={'aria-label': 'Submit Financial Health Form'})

class NetWorthForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa'), ('Yoruba', 'Yoruba'), ('Igbo', 'Igbo')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    assets = FloatField('Total Assets (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦500,000', 'aria-label': 'Total Assets', 'data-tooltip': 'Enter the total value of your assets.'})
    liabilities = FloatField('Total Liabilities (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦200,000', 'aria-label': 'Total Liabilities', 'data-tooltip': 'Enter the total value of your liabilities.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Get My Net Worth', render_kw={'aria-label': 'Submit Net Worth Form'})

class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa'), ('Yoruba', 'Yoruba'), ('Igbo', 'Igbo')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    q1 = SelectField('Track Income/Expenses', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Track Income/Expenses', 'data-tooltip': 'Do you track your income and expenses?'})
    q2 = SelectField('Save vs Spend', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Save vs Spend', 'data-tooltip': 'Do you save a portion of your income?'})
    q3 = SelectField('Financial Risks', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Financial Risks', 'data-tooltip': 'Are you comfortable with financial risks?'})
    q4 = SelectField('Emergency Fund', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Emergency Fund', 'data-tooltip': 'Do you have an emergency fund?'})
    q5 = SelectField('Review Goals', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()], render_kw={'aria-label': 'Review Goals', 'data-tooltip': 'Do you regularly review your financial goals?'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Submit Quiz', render_kw={'aria-label': 'Submit Quiz Form'})

class EmergencyFundForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa'), ('Yoruba', 'Yoruba'), ('Igbo', 'Igbo')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
    monthly_expenses = FloatField('Monthly Essential Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)], render_kw={'placeholder': 'e.g. ₦50,000', 'aria-label': 'Monthly Essential Expenses', 'data-tooltip': 'Enter your monthly essential expenses.'})
    record_id = SelectField('Select Record to Edit', choices=[('', 'Create New Record')], validators=[Optional()], render_kw={'aria-label': 'Select Record', 'data-tooltip': 'Select a previous record to edit or create a new one.'})
    submit = SubmitField('Calculate Emergency Fund', render_kw={'aria-label': 'Submit Emergency Fund Form'})

class BudgetForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()], render_kw={'placeholder': 'e.g. John', 'aria-label': 'First Name', 'data-tooltip': 'Enter your first name.'})
    email = EmailField('Email', validators=[DataRequired(), Email()], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Email', 'data-tooltip': 'Enter your email address.'})
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email(), EqualTo('email', message='Emails must match')], render_kw={'placeholder': 'e.g. john.doe@example.com', 'aria-label': 'Confirm Email', 'data-tooltip': 'Re-enter your email to confirm.'})
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa'), ('Yoruba', 'Yoruba'), ('Igbo', 'Igbo')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
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
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa'), ('Yoruba', 'Yoruba'), ('Igbo', 'Igbo')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
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
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa'), ('Yoruba', 'Yoruba'), ('Igbo', 'Igbo')], validators=[DataRequired()], render_kw={'aria-label': 'Language', 'data-tooltip': 'Select your preferred language.'})
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
@celery.task
def send_email_async(subject, recipients, html, language='English'):
    trans = translations.get(language, translations['English'])
    try:
        msg = Message(subject, sender='ficore.ai.africa@gmail.com', recipients=recipients)
        msg.html = html
        with app.app_context():
            mail.send(msg)
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error: {e}")
    except SMTPException as e:
        logger.error(f"SMTP error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")

@celery.task
def send_bill_reminder_email(bill_json):
    bill = json.loads(bill_json)
    language = bill.get('Language', 'English')
    trans = translations.get(language, translations['English'])
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
            translations=trans
        )
        send_email_async.delay(
            trans['Bill Reminder Subject'].format(description=bill.get('Description', '')),
            [bill.get('Email', '')],
            html,
            language
        )
    except Exception as e:
        logger.error(f"Error sending bill reminder: {e}")

@celery.task
def check_bill_reminders():
    try:
        reminders = sheets['BillReminders'].get_all_records()
        now = datetime.now()
        for reminder in reminders:
            if reminder.get('Status') != 'Pending':
                continue
            reminder_date = parse(reminder.get('ReminderDate', '1970-01-01'))
            if reminder_date <= now:
                bill = get_record_by_id(reminder.get('BillTimestamp'), 'BillPlanner')
                if bill and bill.get('Status') == 'Pending':
                    send_bill_reminder_email.delay(json.dumps(bill))
                reminder['Status'] = 'Sent'
                update_or_append_user_data(reminder, 'BillReminders')
    except Exception as e:
        logger.error(f"Error checking bill reminders: {e}")

# Schedule bill reminder using Google Sheets with date validation
def schedule_bill_reminder(bill):
    language = bill.get('Language', 'English')
    trans = translations.get(language, translations['English'])
    try:
        # Validate the due date format
        due_date_str = bill.get('DueDate', '')
        try:
            due_date = parse(due_date_str)
            due_date_str = due_date.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            logger.error(f"Invalid due date format for bill: {due_date_str}")
            flash(trans['Invalid due date format in bill'], 'error')
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
            flash(translations[language]['Reminder date is in the past'], 'warning')
    except Exception as e:
        logger.error(f"Error scheduling bill reminder: {e}")
        flash(translations[language]['Failed to schedule bill reminder due to server error'], 'error')

# Calculate health score
def calculate_health_score(income, expenses, debt, interest_rate):
    try:
        score = 100
        expense_ratio = expenses / income if income > 0 else 1
        debt_ratio = debt / income if income > 0 else 1
        score -= min(expense_ratio * 40, 40)
        score -= min(debt_ratio * 30, 30)
        if interest_rate:
            score -= min(interest_rate * 0.5, 20)
        return max(0, min(100, round(score)))
    except ZeroDivisionError:
        return 0
    except Exception as e:
        logger.error(f"Error calculating health score: {e}")
        return 50

# Get score description
def get_score_description(score, language='English'):
    trans = translations.get(language, translations['English'])
    if score >= 80:
        return trans['Strong Financial Health']
    elif score >= 60:
        return trans['Stable Finances']
    elif score >= 40:
        return trans['Financial Strain']
    else:
        return trans['Urgent Attention Needed']

# Assign rank for health score
@cache.memoize(timeout=300)
def assign_rank(score):
    try:
        all_scores = [parse_number(row.get('Score', 0)) for row in sheets['HealthScore'].get_all_records() if row.get('Score') and row.get('Score').strip()]
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
    trans = translations.get(language, translations['English'])
    badges = []
    try:
        if score >= 60:
            badges.append(trans['Financial Stability Achieved!'])
        if debt == 0:
            badges.append(trans['Debt Slayer!'])
        if income > 0:
            badges.append(trans['First Health Score Completed!'])
        if score >= 80:
            badges.append(trans['High Value Badge'])
        elif score >= 60:
            badges.append(trans['Positive Value Badge'])
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
        flash(translations.get(language, translations['English'])['Language changed successfully'], 'success')
    else:
        flash(translations.get(session.get('language', 'English'), translations['English'])['Invalid language selection'], 'error')
    return redirect(request.referrer or url_for('index'))

@app.route('/health_score_form', methods=['GET', 'POST'])
def health_score_form():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    email = session.get('user_email')
    form_data = None
    record_choices = [('', trans['Create New Record'])]
    user_records = get_user_data_by_email(email, 'HealthScore') if email else []
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
    form = UserForm(
        first_name=form_data.get('FirstName') if form_data else None,
        last_name=form_data.get('LastName') if form_data else None,
        email=email,
        phone=form_data.get('PhoneNumber') if form_data else None,
        language=form_data.get('Language') if form_data else language,
        business_name=form_data.get('BusinessName') if form_data else None,
        user_type=form_data.get('UserType') if form_data else None,
        income=form_data.get('IncomeRevenue') if form_data else None,
        expenses=form_data.get('ExpensesCosts') if form_data else None,
        debt=form_data.get('DebtLoan') if form_data else None,
        interest_rate=form_data.get('DebtInterestRate') if form_data else None,
        auto_email=form_data.get('AutoEmail', False) if form_data else False,
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
            session.permanent = True
            income = parse_number(form.income.data)
            expenses = parse_number(form.expenses.data)
            debt = parse_number(form.debt.data)
            interest_rate = parse_number(form.interest_rate.data or 0)
            health_score = calculate_health_score(income, expenses, debt, interest_rate)
            score_description = get_score_description(health_score, language)
            rank, total_users = assign_rank(health_score)
            badges = assign_badges(health_score, debt, income, language)
            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'BusinessName': form.business_name.data,
                'IncomeRevenue': income,
                'ExpensesCosts': expenses,
                'DebtLoan': debt,
                'DebtInterestRate': interest_rate,
                'AutoEmail': str(form.auto_email.data),
                'PhoneNumber': form.phone.data or '',
                'FirstName': form.first_name.data,
                'LastName': form.last_name.data or '',
                'UserType': form.user_type.data,
                'Email': form.email.data,
                'Badges': ','.join(badges),
                'Language': form.language.data,
                'Score': health_score
            }
            if form.record_id.data:
                user_data['Timestamp'] = form.record_id.data
            update_or_append_user_data(user_data, 'HealthScore')
            if form.auto_email.data:
                html = render_template(
                    'email_templates/health_score_email.html',
                    user_name=form.first_name.data,
                    health_score=health_score,
                    score_description=score_description,
                    rank=rank,
                    total_users=total_users,
                    course_title=trans['Recommended Course'],
                    course_url='https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru',
                    FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                    WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                    CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
                    translations=trans
                )
                send_email_async.delay(
                    trans['Score Report Subject'].format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(trans['Email scheduled to be sent'], 'success')
            session['user_data_id'] = user_data['Timestamp']
            chart_html, comparison_chart_html = generate_health_score_charts(json.dumps(user_data), language)
            return redirect(url_for('health_score_dashboard', user_data=json.dumps(user_data), chart_html=chart_html, comparison_chart_html=comparison_chart_html, score_description=score_description, rank=rank, total_users=total_users, badges=json.dumps(badges)))
    return render_template(
        'health_score_form.html',
        form=form,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/health_score_dashboard')
def health_score_dashboard():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    user_data = json.loads(request.args.get('user_data', '{}'))
    chart_html = request.args.get('chart_html', '')
    comparison_chart_html = request.args.get('comparison_chart_html', '')
    score_description = request.args.get('score_description', '')
    rank = request.args.get('rank', '1')
    total_users = request.args.get('total_users', '1')
    badges = json.loads(request.args.get('badges', '[]'))
    return render_template(
        'health_score_dashboard.html',
        tool='Financial Health Score',
        user_data=user_data,
        chart_html=chart_html,
        comparison_chart_html=comparison_chart_html,
        score_description=score_description,
        rank=rank,
        total_users=total_users,
        badges=badges,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/net_worth_form', methods=['GET', 'POST'])
def net_worth_form():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    email = session.get('user_email')
    form_data = None
    record_choices = [('', trans['Create New Record'])]
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
            session.permanent = True
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
            if form.auto_email.data:
                html = render_template(
                    'email_templates/net_worth_email.html',
                    user_name=form.first_name.data,
                    net_worth=net_worth,
                    rank_percentile=rank_percentile,
                    badges=badges,
                    advice=advice,
                    FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                    WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                    CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
                    translations=trans
                )
                send_email_async.delay(
                    trans['Net Worth Report Subject'].format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(trans['Email scheduled to be sent'], 'success')
            chart_html, comparison_chart_html = generate_net_worth_charts(json.dumps(user_data), language)
            return redirect(url_for('net_worth_dashboard', user_data=json.dumps(user_data), chart_html=chart_html, comparison_chart_html=comparison_chart_html, rank_percentile=rank_percentile, badges=json.dumps(badges), advice=advice))
    return render_template(
        'net_worth_form.html',
        form=form,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/net_worth_dashboard')
def net_worth_dashboard():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    user_data = json.loads(request.args.get('user_data', '{}'))
    chart_html = request.args.get('chart_html', '')
    comparison_chart_html = request.args.get('comparison_chart_html', '')
    rank_percentile = request.args.get('rank_percentile', '50.0')
    badges = json.loads(request.args.get('badges', '[]'))
    advice = request.args.get('advice', '')
    return render_template(
        'net_worth_dashboard.html',
        tool='Net Worth',
        user_data=user_data,
        chart_html=chart_html,
        comparison_chart_html=comparison_chart_html,
        rank_percentile=rank_percentile,
        badges=badges,
        advice=advice,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/quiz_form', methods=['GET', 'POST'])
def quiz_form():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    email = session.get('user_email')
    form_data = None
    record_choices = [('', trans['Create New Record'])]
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
            session.permanent = True
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
                    translations=trans
                )
                send_email_async.delay(
                    trans['Quiz Report Subject'].format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(trans['Email scheduled to be sent'], 'success')
            return redirect(url_for('quiz_dashboard', user_data=json.dumps(user_data), score=score, personality=personality, badges=json.dumps(badges), advice=advice))
    return render_template(
        'quiz_form.html',
        form=form,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/quiz_dashboard')
def quiz_dashboard():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
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
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/emergency_fund_form', methods=['GET', 'POST'])
def emergency_fund_form():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    email = session.get('user_email')
    form_data = None
    record_choices = [('', trans['Create New Record'])]
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
            session.permanent = True
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
                    translations=trans
                )
                send_email_async.delay(
                    trans['Emergency Fund Report Subject'].format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(trans['Email scheduled to be sent'], 'success')
            return redirect(url_for('emergency_fund_dashboard', user_data=json.dumps(user_data)))
    return render_template(
        'emergency_fund_form.html',
        form=form,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/emergency_fund_dashboard')
def emergency_fund_dashboard():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    user_data = json.loads(request.args.get('user_data', '{}'))
    return render_template(
        'emergency_fund_dashboard.html',
        tool='Emergency Fund',
        user_data=user_data,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/budget_form', methods=['GET', 'POST'])
def budget_form():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    email = session.get('user_email')
    form_data = None
    record_choices = [('', trans['Create New Record'])]
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
        auto_email=form_data.get('AutoEmail', False) if form_data else False,
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
            session.permanent = True
            income = parse_number(form.income.data)
            housing = parse_number(form.housing.data)
            food = parse_number(form.food.data)
            transport = parse_number(form.transport.data)
            other = parse_number(form.other.data)
            total_expenses = housing + food + transport + other
            savings = max(0, income - total_expenses)
            surplus_deficit = income - total_expenses
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
                    FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                    WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                    CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
                    translations=trans
                )
                send_email_async.delay(
                    trans['Budget Report Subject'].format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(trans['Email scheduled to be sent'], 'success')
            chart_html = generate_budget_charts(json.dumps(user_data), language)
            return redirect(url_for('budget_dashboard', user_data=json.dumps(user_data), chart_html=chart_html))
    return render_template(
        'budget_form.html',
        form=form,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/budget_dashboard')
def budget_dashboard():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    user_data = json.loads(request.args.get('user_data', '{}'))
    chart_html = request.args.get('chart_html', '')
    return render_template(
        'budget_dashboard.html',
        tool='Budget Planner',
        user_data=user_data,
        chart_html=chart_html,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/expense_tracker_form', methods=['GET', 'POST'])
def expense_tracker_form():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    email = session.get('user_email')
    form_data = None
    record_choices = [('', trans['Create New Record'])]
    user_records = get_user_data_by_email(email, 'ExpenseTracker') if email else []
    if user_records:
        for record in user_records:
            record_id = record.get('ID', 'Unknown')
            record_choices.append((record_id, f"Record ID {record_id}"))
    selected_record_id = request.args.get('record_id', '') if request.method == 'GET' else None
    if selected_record_id and email:
        for record in user_records:
            if record.get('ID') == selected_record_id:
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
        auto_email=form_data.get('AutoEmail', False) if form_data else False,
        record_id=selected_record_id
    )
    form.record_id.choices = record_choices
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
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
                'RunningBalance': calculate_running_balance(form.email.data)
            }
            if form.record_id.data:
                user_data['ID'] = form.record_id.data
            update_or_append_user_data(user_data, 'ExpenseTracker')
            if form.auto_email.data:
                html = render_template(
                    'email_templates/expense_email.html',
                    user_name=form.first_name.data,
                    amount=amount,
                    category=form.category.data,
                    description=form.description.data,
                    date=user_data['Date'],
                    transaction_type=form.transaction_type.data,
                    running_balance=user_data['RunningBalance'],
                    FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                    WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                    CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
                    translations=trans
                )
                send_email_async.delay(
                    trans['Expense Report Subject'].format(user_name=form.first_name.data),
                    [form.email.data],
                    html,
                    language
                )
                flash(trans['Email scheduled to be sent'], 'success')
            chart_html = generate_expense_charts(form.email.data, language)
            return redirect(url_for('expense_tracker_dashboard', user_data=json.dumps(user_data), chart_html=chart_html))
    return render_template(
        'expense_tracker_form.html',
        form=form,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/expense_tracker_dashboard')
def expense_tracker_dashboard():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    user_data = json.loads(request.args.get('user_data', '{}'))
    chart_html = request.args.get('chart_html', '')
    return render_template(
        'expense_tracker_dashboard.html',
        tool='Expense Tracker',
        user_data=user_data,
        chart_html=chart_html,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/bill_planner_form', methods=['GET', 'POST'])
def bill_planner_form():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    email = session.get('user_email')
    form_data = None
    record_choices = [('', trans['Create New Record'])]
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
            session.permanent = True
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
                flash(trans['Bill reminder scheduled'], 'success')
            return redirect(url_for('bill_planner_dashboard', user_data=json.dumps(user_data)))
    return render_template(
        'bill_planner_form.html',
        form=form,
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/bill_planner_dashboard')
def bill_planner_dashboard():
    language = session.get('language', 'English')
    trans = translations.get(language, translations['English'])
    user_data = json.loads(request.args.get('user_data', '{}'))
    return render_template(
        'bill_planner_dashboard.html',
        tool='Bill Planner',
        user_data=user_data,
        tips=get_tips(language),
        courses=get_courses(language),
        translations=trans,
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/logout')
def logout():
    session.pop('user_email', None)
    session.pop('language', None)
    session.pop('user_data_id', None)
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Cleanup Redis connection on exit
    redis_client = redis.Redis(host='localhost', port=6379, db=0)
    atexit.register(redis_client.close)
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
