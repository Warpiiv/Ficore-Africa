import os
import uuid
import json
import re
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, TextAreaField, EmailField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Email, Optional, NumberRange
from flask_mail import Mail, Message
from smtplib import SMTPException, SMTPAuthenticationError
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dateutil.parser import parse
import random
import numpy as np
import plotly.graph_objects as go
import plotly.io as pio
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
import atexit
from translations import translations

logging.basicConfig(filename='app.log', level=logging.ERROR, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'NEscD7rN4cuYR3o3VLZZuSj3myhwAX7')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'ficore.ai.africa@gmail.com'
app.config['MAIL_PASSWORD'] = os.environ.get('SMTP_PASSWORD')
if not app.config['MAIL_PASSWORD']:
    logger.error("SMTP_PASSWORD environment variable not set")
mail = Mail(app)

scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
try:
    creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
except Exception as e:
    logger.error(f"Failed to initialize Google Sheets credentials: {e}")

try:
    spreadsheet = client.open_by_key('13hbiMTMRBHo9MHjWwcugngY_aSiuxII67HCf03MiZ8I')
except Exception as e:
    logger.error(f"Error accessing Google Sheets: {e}")

WORKSHEETS = {
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
    }
}

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

def parse_number(value):
    try:
        if isinstance(value, str):
            value = value.replace(',', '')
        return float(value)
    except (ValueError, TypeError):
        return 0

def get_user_data_by_email(email, tool):
    try:
        records = sheets[tool].get_all_records()
        for record in records:
            if record.get('Email') == email or record.get('UserEmail') == email:
                return record
        return None
    except Exception as e:
        logger.error(f"Error fetching user data from {WORKSHEETS[tool]['name']}: {e}")
        return None

def update_or_append_user_data(user_data, tool):
    language = session.get('language', 'English')
    sheet = sheets[tool]
    headers = WORKSHEETS[tool]['headers']
    try:
        records = sheet.get_all_records()
        email = user_data.get('Email') or user_data.get('UserEmail')
        if email:
            for i, record in enumerate(records, start=2):
                if record.get('Email') == email or record.get('UserEmail') == email:
                    existing_data = record
                    merged_data = {**existing_data, **user_data}
                    sheet.update(f'A{i}:{chr(64 + len(headers))}{i}', [[merged_data.get(header, '') for header in headers]])
                    return
        sheet.append_row([user_data.get(header, '') for header in headers])
    except gspread.exceptions.APIError as e:
        logger.error(f"Google Sheets API error updating/appending data to {WORKSHEETS[tool]['name']}: {e}")
        flash(translations[language]['Failed to save data due to Google Sheets error'], 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])
    except Exception as e:
        logger.error(f"Error updating/appending data to {WORKSHEETS[tool]['name']}: {e}")
        flash(translations[language]['Failed to save data'], 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])

def assign_net_worth_rank(net_worth):
    try:
        all_net_worths = [float(row['NetWorth']) for row in sheets['NetWorth'].get_all_records() if row['NetWorth'] and row['NetWorth'].strip()]
        all_net_worths.append(net_worth)
        rank_percentile = 100 - np.percentile(all_net_worths, np.searchsorted(sorted(all_net_worths, reverse=True), net_worth) / len(all_net_worths) * 100)
        return round(rank_percentile, 1)
    except Exception as e:
        logger.error(f"Error assigning net worth rank: {e}")
        return 50.0

def get_net_worth_advice(net_worth, language='English'):
    if net_worth > 0:
        return translations[language]['Maintain your positive net worth by continuing to manage liabilities and grow assets.']
    elif net_worth == 0:
        return translations[language]['Your net worth is balanced. Consider increasing assets to build wealth.']
    else:
        return translations[language]['Focus on reducing liabilities to improve your net worth.']

def assign_net_worth_badges(net_worth, language='English'):
    badges = []
    try:
        if net_worth > 0:
            badges.append(translations[language]['Positive Net Worth'])
        if net_worth >= 100000:
            badges.append(translations[language]['Wealth Builder'])
        if net_worth <= -50000:
            badges.append(translations[language]['Debt Recovery'])
    except Exception as e:
        logger.error(f"Error assigning net worth badges: {e}")
    return badges

def get_tips(language='English'):
    return [
        translations[language]['Regularly review your assets and liabilities to track progress.'],
        translations[language]['Invest in low-risk assets to grow your wealth steadily.'],
        translations[language]['Create a plan to pay down high-interest debt first.']
    ]

def get_courses(language='English'):
    return [
        {'title': translations[language]['Personal Finance 101'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': translations[language]['Debt Management Basics'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': translations[language]['Investing for Beginners'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'}
    ]

def get_quiz_advice(score, personality, language='English'):
    if score >= 4:
        return translations[language]['Great job! Continue to leverage your {personality} approach to build wealth.'].format(personality=personality.lower())
    elif score >= 2:
        return translations[language]['Good effort! Your {personality} style is solid, but consider tracking expenses more closely.'].format(personality=personality.lower())
    else:
        return translations[language]['Keep learning! Your {personality} approach can improve with regular financial reviews.'].format(personality=personality.lower())

def assign_quiz_badges(score, language='English'):
    badges = []
    try:
        if score >= 4:
            badges.append(translations[language]['Financial Guru'])
        if score >= 2:
            badges.append(translations[language]['Quiz Achiever'])
        badges.append(translations[language]['Quiz Participant'])
    except Exception as e:
        logger.error(f"Error assigning quiz badges: {e}")
    return badges

def get_average_health_score():
    try:
        records = sheets['HealthScore'].get_all_records()
        scores = [float(row['Score']) for row in records if row['Score'] and row['Score'].strip()]
        return np.mean(scores) if scores else 50
    except Exception as e:
        logger.error(f"Error calculating average health score: {e}")
        return 50

def generate_health_score_charts(user_data, language='English'):
    try:
        income = user_data['IncomeRevenue']
        debt = user_data['DebtLoan']
        ratio_message = translations[language]['Your asset-to-liability ratio is healthy.'] if income >= 2 * debt else translations[language]['Your liabilities are high. Consider strategies to reduce debt.']
        bar_fig = go.Figure(data=[
            go.Bar(name=translations[language]['Income (₦)'], x=['Income'], y=[income], marker_color='#2E7D32'),
            go.Bar(name=translations[language]['Debt (₦)'], x=['Debt'], y=[debt], marker_color='#DC3545')
        ])
        bar_fig.update_layout(
            title=translations[language]['Asset-Liability Breakdown'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            annotations=[dict(text=ratio_message, xref="paper", yref="paper", x=0.5, y=1.1, showarrow=False)]
        )
        chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=False)

        average_score = get_average_health_score()
        bar_fig = go.Figure(data=[
            go.Bar(name=translations[language]['Your Score'], x=['You'], y=[user_data['Score']], marker_color='#2E7D32'),
            go.Bar(name=translations[language]['Average Score'], x=['Average'], y=[average_score], marker_color='#0288D1')
        ])
        bar_fig.update_layout(
            title=translations[language]['Comparison to Peers'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        comparison_chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=False)

        return chart_html, comparison_chart_html
    except Exception as e:
        logger.error(f"Error generating health score charts: {e}")
        return "", ""

def generate_net_worth_charts(net_worth_data, language='English'):
    try:
        labels = [translations[language]['Assets'], translations[language]['Liabilities']]
        values = [net_worth_data['Assets'], net_worth_data['Liabilities']]
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545']))])
        pie_fig.update_layout(
            title=translations[language]['Asset-Liability Breakdown'],
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=False)

        all_net_worths = [float(row['NetWorth']) for row in sheets['NetWorth'].get_all_records() if row['NetWorth'] and row['NetWorth'].strip()]
        user_net_worth = net_worth_data['NetWorth']
        avg_net_worth = np.mean(all_net_worths) if all_net_worths else 0
        bar_fig = go.Figure(data=[
            go.Bar(name=translations[language]['Your Net Worth'], x=['You'], y=[user_net_worth], marker_color='#2E7D32'),
            go.Bar(name=translations[language]['Average Net Worth'], x=['Average'], y=[avg_net_worth], marker_color='#0288D1')
        ])
        bar_fig.update_layout(
            title=translations[language]['Comparison to Peers'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        comparison_chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=False)

        return chart_html, comparison_chart_html
    except Exception as e:
        logger.error(f"Error generating net worth charts: {e}")
        return "", ""

def generate_budget_charts(budget_data, language='English'):
    try:
        labels = [
            translations[language]['Housing'],
            translations[language]['Food'],
            translations[language]['Transport'],
            translations[language]['Other'],
            translations[language]['Savings']
        ]
        values = [
            budget_data['HousingExpenses'],
            budget_data['FoodExpenses'],
            budget_data['TransportExpenses'],
            budget_data['OtherExpenses'],
            max(budget_data['Savings'], 0)
        ]
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545', '#0288D1', '#FFB300', '#4CAF50']))])
        pie_fig.update_layout(
            title=translations[language]['Budget Breakdown'],
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=False)
        return chart_html
    except Exception as e:
        logger.error(f"Error generating budget charts: {e}")
        return ""

class UserForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    last_name = StringField('Last Name', validators=[Optional()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email()])
    phone = StringField('Phone Number', validators=[Optional()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    business_name = StringField('Business Name', validators=[DataRequired()])
    user_type = SelectField('User Type', choices=[('Individual', 'Individual'), ('Business', 'Business')], validators=[DataRequired()])
    income = FloatField('Monthly Income (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    expenses = FloatField('Monthly Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    debt = FloatField('Total Debt (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    interest_rate = FloatField('Debt Interest Rate (%)', validators=[Optional(), NumberRange(min=0, max=100)])
    auto_email = BooleanField('Send Email Notification', default=False)
    submit = SubmitField('Check My Financial Health')

class NetWorthForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    assets = FloatField('Total Assets (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    liabilities = FloatField('Total Liabilities (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    submit = SubmitField('Get My Net Worth')

class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    q1 = SelectField('Track Income/Expenses', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q2 = SelectField('Save vs Spend', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q3 = SelectField('Financial Risks', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q4 = SelectField('Emergency Fund', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q5 = SelectField('Review Goals', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    submit = SubmitField('Submit Quiz')

class EmergencyFundForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    monthly_expenses = FloatField('Monthly Essential Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    submit = SubmitField('Calculate Emergency Fund')

class BudgetForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    income = FloatField('Total Monthly Income (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    housing = FloatField('Housing Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    food = FloatField('Food Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    transport = FloatField('Transport Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    other = FloatField('Other Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    auto_email = BooleanField('Send Email Notification', default=False)
    submit = SubmitField('Plan My Budget')

class ExpenseForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    amount = FloatField('Amount (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    description = TextAreaField('Description', validators=[DataRequired()])
    category = SelectField('Category', choices=[
        ('Food and Groceries', 'Food and Groceries'),
        ('Transport', 'Transport'),
        ('Housing', 'Housing'),
        ('Utilities', 'Utilities'),
        ('Entertainment', 'Entertainment'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    transaction_type = SelectField('Transaction Type', choices=[('Income', 'Income'), ('Expense', 'Expense')], validators=[DataRequired()])
    submit = SubmitField('Add Transaction')

class BillForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    description = TextAreaField('Description', validators=[DataRequired()])
    amount = FloatField('Amount (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    due_date = StringField('Due Date', validators=[DataRequired()])
    category = SelectField('Category', choices=[
        ('Utilities', 'Utilities'),
        ('Housing', 'Housing'),
        ('Transport', 'Transport'),
        ('Food', 'Food'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    recurrence = SelectField('Recurrence', choices=[
        ('None', 'None'),
        ('Daily', 'Daily'),
        ('Weekly', 'Weekly'),
        ('Monthly', 'Monthly'),
        ('Yearly', 'Yearly')
    ], validators=[DataRequired()])
    send_email = BooleanField('Send Email Notification', default=False)
    submit = SubmitField('Add Bill')

if APSCHEDULER_AVAILABLE:
    scheduler = BackgroundScheduler()
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

def schedule_bill_reminder(bill):
    try:
        due_date = parse(bill['DueDate'])
        reminder_date = due_date - timedelta(days=1)
        if reminder_date > datetime.now():
            scheduler.add_job(
                send_bill_reminder_email,
                'date',
                run_date=reminder_date,
                args=[bill],
                id=f"bill_reminder_{bill['Timestamp']}"
            )
    except Exception as e:
        logger.error(f"Error scheduling bill reminder: {e}")

def send_bill_reminder_email(bill):
    language = bill.get('Language', 'English')
    try:
        msg = Message(
            translations[language]['Bill Reminder Subject'].format(description=bill['Description']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[bill['Email']]
        )
        msg.html = translations[language]['Bill Reminder Email Body'].format(
            user_name=bill['FirstName'],
            description=bill['Description'],
            amount=bill['Amount'],
            due_date=bill['DueDate'],
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending bill reminder: {e}")
    except SMTPException as e:
        logger.error(f"SMTP error sending bill reminder: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending bill reminder: {e}")

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

def get_score_description(score):
    language = session.get('language', 'English')
    if score >= 80:
        return translations[language]['Strong Financial Health']
    elif score >= 60:
        return translations[language]['Stable Finances']
    elif score >= 40:
        return translations[language]['Financial Strain']
    else:
        return translations[language]['Urgent Attention Needed']

def assign_rank(score):
    try:
        all_scores = [float(row['Score']) for row in sheets['HealthScore'].get_all_records() if row['Score'] and row['Score'].strip()]
        all_scores.append(score)
        sorted_scores = sorted(all_scores, reverse=True)
        rank = sorted_scores.index(score) + 1
        total_users = len(all_scores)
        return rank, total_users
    except Exception as e:
        logger.error(f"Error assigning rank: {e}")
        return 1, 1

def assign_badges(score, debt, income):
    language = session.get('language', 'English')
    badges = []
    try:
        if score >= 60:
            badges.append(translations[language]['Financial Stability Achieved!'])
        if debt == 0:
            badges.append(translations[language]['Debt Slayer!'])
        if income > 0:
            badges.append(translations[language]['First Health Score Completed!'])
        if score >= 80:
            badges.append(translations[language]['High Value Badge'])
        elif score >= 60:
            badges.append(translations[language]['Positive Value Badge'])
    except Exception as e:
        logger.error(f"Error assigning badges: {e}")
    return badges

@app.route('/', methods=['GET'])
def index():
    return redirect(url_for('landing'))

@app.route('/landing')
def landing():
    language = session.get('language', 'English')
    return render_template('landing.html', translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/change_language', methods=['POST'])
def change_language():
    language = request.form.get('language', 'English')
    if language in ['English', 'Hausa']:
        session['language'] = language
        flash(translations[language]['Language changed successfully'], 'success')
    else:
        flash(translations[session.get('language', 'English')]['Invalid language selection'], 'error')
    return redirect(request.referrer or url_for('landing'))

@app.route('/Health_Score_Form', methods=['GET', 'POST'])
def Health_Score_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'HealthScore') if email else None
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
        auto_email=form_data.get('AutoEmail', False) if form_data else False
    )
    if email:
        form.email.render_kw = {'readonly': True}
        form.confirm_email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if form.email.data != form.confirm_email.data:
                flash(translations[language]['Emails Do Not Match'], 'error')
                return render_template('health_score_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            income = parse_number(form.income.data)
            expenses = parse_number(form.expenses.data)
            debt = parse_number(form.debt.data)
            interest_rate = parse_number(form.interest_rate.data or 0)
            health_score = calculate_health_score(income, expenses, debt, interest_rate)
            score_description = get_score_description(health_score)
            rank, total_users = assign_rank(health_score)
            badges = assign_badges(health_score, debt, income)
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
            update_or_append_user_data(user_data, 'HealthScore')
            if form.auto_email.data:
                try:
                    msg = Message(
                        translations[language]['Score Report Subject'].format(user_name=form.first_name.data),
                        sender='ficore.ai.africa@gmail.com',
                        recipients=[form.email.data]
                    )
                    course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
                    course_title = translations[language]['Recommended Course']
                    msg.html = translations[language]['Email Body'].format(
                        user_name=form.first_name.data,
                        health_score=health_score,
                        score_description=score_description,
                        rank=rank,
                        total_users=total_users,
                        course_url=course_url,
                        course_title=course_title,
                        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
                    )
                    mail.send(msg)
                    flash(translations[language]['Email sent successfully'], 'success')
                except SMTPAuthenticationError as e:
                    logger.error(f"SMTP authentication error sending health score email: {e}")
                    flash(translations[language]['Failed to send email due to authentication issue'], 'error')
                except SMTPException as e:
                    logger.error(f"SMTP error sending health score email: {e}")
                    flash(translations[language]['Failed to send email due to server issue'], 'error')
                except Exception as e:
                    logger.error(f"Unexpected error sending health score email: {e}")
                    flash(translations[language]['Failed to send email'], 'error')
            session['user_data'] = user_data
            session['score_description'] = score_description
            session['badges'] = badges
            session['average_score'] = get_average_health_score()
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('health_score_dashboard') + '?success=true')
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('health_score_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/health_score_dashboard')
def health_score_dashboard():
    language = session.get('language', 'English')
    user_data = session.get('user_data')
    score_description = session.get('score_description')
    badges = session.get('badges', [])
    average_score = session.get('average_score', 50)
    tips = get_tips(language)
    courses = get_courses(language)
    if not user_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Health_Score_Form'))
    chart_html, comparison_chart_html = generate_health_score_charts(user_data, language)
    return render_template(
        'health_score_dashboard.html',
        user_data=user_data,
        score_description=score_description,
        badges=badges,
        average_score=average_score,
        tips=tips,
        courses=courses,
        chart_html=chart_html,
        comparison_chart_html=comparison_chart_html,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_health_score_email')
def send_health_score_email():
    language = session.get('language', 'English')
    user_data = session.get('user_data')
    score_description = session.get('score_description')
    if not user_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Health_Score_Form'))
    try:
        msg = Message(
            translations[language]['Score Report Subject'].format(user_name=user_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[user_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        rank, total_users = assign_rank(user_data['Score'])
        msg.html = translations[language]['Email Body'].format(
            user_name=user_data['FirstName'],
            health_score=user_data['Score'],
            score_description=score_description,
            rank=rank,
            total_users=total_users,
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending health score email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending health score email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending health score email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('health_score_dashboard'))

@app.route('/Net_Worth_Form', methods=['GET', 'POST'])
def Net_Worth_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'NetWorth') if email else None
    form = NetWorthForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        assets=form_data.get('Assets', 0) if form_data else 0,
        liabilities=form_data.get('Liabilities', 0) if form_data else 0
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('net_worth_form.html', form=form, net_worth=0, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            try:
                assets = parse_number(form.assets.data)
                liabilities = parse_number(form.liabilities.data)
                net_worth = assets - liabilities
                user_data = {
                    'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'FirstName': form.first_name.data,
                    'Email': form.email.data,
                    'Language': form.language.data,
                    'Assets': assets,
                    'Liabilities': liabilities,
                    'NetWorth': net_worth
                }
                update_or_append_user_data(user_data, 'NetWorth')
                session['net_worth_data'] = user_data
                flash(translations[language]['Submission Success'], 'success')
                return redirect(url_for('net_worth_dashboard'))
            except Exception as e:
                logger.error(f"Error calculating net worth: {e}")
                flash(translations[language]['Error calculating net worth'], 'error')
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('net_worth_form.html', form=form, net_worth=0, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/net_worth_dashboard')
def net_worth_dashboard():
    language = session.get('language', 'English')
    net_worth_data = session.get('net_worth_data')
    if not net_worth_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Net_Worth_Form'))
    rank = assign_net_worth_rank(net_worth_data['NetWorth'])
    advice = get_net_worth_advice(net_worth_data['NetWorth'], language)
    badges = assign_net_worth_badges(net_worth_data['NetWorth'], language)
    tips = get_tips(language)
    courses = get_courses(language)
    chart_html, comparison_chart_html = generate_net_worth_charts(net_worth_data, language)
    return render_template(
        'net_worth_dashboard.html',
        net_worth_data=net_worth_data,
        rank=rank,
        advice=advice,
        badges=badges,
        tips=tips,
        courses=courses,
        chart_html=chart_html,
        comparison_chart_html=comparison_chart_html,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_net_worth_email')
def send_net_worth_email():
    language = session.get('language', 'English')
    net_worth_data = session.get('net_worth_data')
    if not net_worth_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Net_Worth_Form'))
    try:
        msg = Message(
            translations[language]['Net Worth Report Subject'].format(user_name=net_worth_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[net_worth_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        rank = assign_net_worth_rank(net_worth_data['NetWorth'])
        msg.html = translations[language]['Net Worth Email Body'].format(
            user_name=net_worth_data['FirstName'],
            net_worth=net_worth_data['NetWorth'],
            assets=net_worth_data['Assets'],
            liabilities=net_worth_data['Liabilities'],
            rank=rank,
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending net worth email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending net worth email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending net worth email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('net_worth_dashboard'))

@app.route('/Quiz_Form', methods=['GET', 'POST'])
def Quiz_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'Quiz') if email else None
    form = QuizForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        q1=form_data.get('Q1') if form_data else None,
        q2=form_data.get('Q2') if form_data else None,
        q3=form_data.get('Q3') if form_data else None,
        q4=form_data.get('Q4') if form_data else None,
        q5=form_data.get('Q5') if form_data else None
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('quiz_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            score = sum(1 for q in [form.q1.data, form.q2.data, form.q3.data, form.q4.data, form.q5.data] if q == 'Yes')
            personality = 'Conservative' if score <= 1 else 'Balanced' if score <= 3 else 'Risk-Taker'
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
            update_or_append_user_data(user_data, 'Quiz')
            session['quiz_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('quiz_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('quiz_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/quiz_dashboard')
def quiz_dashboard():
    language = session.get('language', 'English')
    quiz_data = session.get('quiz_data')
    if not quiz_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Quiz_Form'))
    score = quiz_data['QuizScore']
    personality = quiz_data['Personality']
    advice = get_quiz_advice(score, personality, language)
    badges = assign_quiz_badges(score, language)
    tips = get_tips(language)
    courses = get_courses(language)
    return render_template(
        'quiz_dashboard.html',
        quiz_data=quiz_data,
        score=score,
        personality=personality,
        advice=advice,
        badges=badges,
        tips=tips,
        courses=courses,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_quiz_email')
def send_quiz_email():
    language = session.get('language', 'English')
    quiz_data = session.get('quiz_data')
    if not quiz_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Quiz_Form'))
    try:
        msg = Message(
            translations[language]['Quiz Report Subject'].format(user_name=quiz_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[quiz_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        msg.html = translations[language]['Quiz Email Body'].format(
            user_name=quiz_data['FirstName'],
            score=quiz_data['QuizScore'],
            personality=quiz_data['Personality'],
            advice=get_quiz_advice(quiz_data['QuizScore'], quiz_data['Personality'], language),
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending quiz email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending quiz email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending quiz email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('quiz_dashboard'))

@app.route('/Emergency_Fund_Form', methods=['GET', 'POST'])
def Emergency_Fund_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'EmergencyFund') if email else None
    form = EmergencyFundForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        monthly_expenses=form_data.get('MonthlyExpenses') if form_data else None
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('emergency_fund_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            monthly_expenses = parse_number(form.monthly_expenses.data)
            recommended_fund = monthly_expenses * 6
            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'Language': form.language.data,
                'MonthlyExpenses': monthly_expenses,
                'RecommendedFund': recommended_fund
            }
            update_or_append_user_data(user_data, 'EmergencyFund')
            session['emergency_fund_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('emergency_fund_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('emergency_fund_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/emergency_fund_dashboard')
def emergency_fund_dashboard():
    language = session.get('language', 'English')
    emergency_fund_data = session.get('emergency_fund_data')
    if not emergency_fund_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Emergency_Fund_Form'))
    tips = get_tips(language)
    courses = get_courses(language)
    return render_template(
        'emergency_fund_dashboard.html',
        emergency_fund_data=emergency_fund_data,
        tips=tips,
        courses=courses,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_emergency_fund_email')
def send_emergency_fund_email():
    language = session.get('language', 'English')
    emergency_fund_data = session.get('emergency_fund_data')
    if not emergency_fund_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Emergency_Fund_Form'))
    try:
        msg = Message(
            translations[language]['Emergency Fund Report Subject'].format(user_name=emergency_fund_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[emergency_fund_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        msg.html = translations[language]['Emergency Fund Email Body'].format(
            user_name=emergency_fund_data['FirstName'],
            monthly_expenses=emergency_fund_data['MonthlyExpenses'],
            recommended_fund=emergency_fund_data['RecommendedFund'],
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending emergency fund email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending emergency fund email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending emergency fund email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('emergency_fund_dashboard'))

@app.route('/Budget_Form', methods=['GET', 'POST'])
def Budget_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'Budget') if email else None
    form = BudgetForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        income=form_data.get('MonthlyIncome') if form_data else None,
        housing=form_data.get('HousingExpenses') if form_data else None,
        food=form_data.get('FoodExpenses') if form_data else None,
        transport=form_data.get('TransportExpenses') if form_data else None,
        other=form_data.get('OtherExpenses') if form_data else None,
        auto_email=form_data.get('AutoEmail', False) if form_data else False
    )
    if email:
        form.email.render_kw = {'readonly': True}
        form.confirm_email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if form.email.data != form.confirm_email.data:
                flash(translations[language]['Emails Do Not Match'], 'error')
                return render_template('budget_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            income = parse_number(form.income.data)
            housing = parse_number(form.housing.data)
            food = parse_number(form.food.data)
            transport = parse_number(form.transport.data)
            other = parse_number(form.other.data)
            total_expenses = housing + food + transport + other
            savings = income * 0.2
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
            update_or_append_user_data(user_data, 'Budget')
            if form.auto_email.data:
                try:
                    msg = Message(
                        translations[language]['Budget Report Subject'].format(user_name=form.first_name.data),
                        sender='ficore.ai.africa@gmail.com',
                        recipients=[form.email.data]
                    )
                    course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
                    course_title = translations[language]['Recommended Course']
                    msg.html = translations[language]['Budget Email Body'].format(
                        user_name=form.first_name.data,
                        income=income,
                        total_expenses=total_expenses,
                        savings=savings,
                        surplus_deficit=surplus_deficit,
                        course_url=course_url,
                        course_title=course_title,
                        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
                    )
                    mail.send(msg)
                    flash(translations[language]['Email sent successfully'], 'success')
                except SMTPAuthenticationError as e:
                    logger.error(f"SMTP authentication error sending budget email: {e}")
                    flash(translations[language]['Failed to send email due to authentication issue'], 'error')
                except SMTPException as e:
                    logger.error(f"SMTP error sending budget email: {e}")
                    flash(translations[language]['Failed to send email due to server issue'], 'error')
                except Exception as e:
                    logger.error(f"Unexpected error sending budget email: {e}")
                    flash(translations[language]['Failed to send email'], 'error')
            session['budget_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('budget_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('budget_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/budget_dashboard')
def budget_dashboard():
    language = session.get('language', 'English')
    budget_data = session.get('budget_data')
    if not budget_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Budget_Form'))
    tips = get_tips(language)
    courses = get_courses(language)
    chart_html = generate_budget_charts(budget_data, language)
    return render_template(
        'budget_dashboard.html',
        budget_data=budget_data,
        tips=tips,
        courses=courses,
        chart_html=chart_html,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_budget_email')
def send_budget_email():
    language = session.get('language', 'English')
    budget_data = session.get('budget_data')
    if not budget_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Budget_Form'))
    try:
        msg = Message(
            translations[language]['Budget Report Subject'].format(user_name=budget_data['FirstName

System: It looks like the `app.py` code was cut off again due to character limits. I'll provide the complete `app.py` file, ensuring all routes and functionalities are included without any truncation. I'll focus solely on the code, as requested, to maximize space for the artifact.

<xaiArtifact artifact_id="e0e44919-6849-49a9-bf6a-8e1feebfd6fd" artifact_version_id="21931e2f-9b96-440b-ba93-d4cce1b67d2e" title="app.py" contentType="text/python">
import os
import uuid
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, TextAreaField, EmailField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Email, Optional, NumberRange
from flask_mail import Mail, Message
from smtplib import SMTPException, SMTPAuthenticationError
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dateutil.parser import parse
import numpy as np
import plotly.graph_objects as go
import plotly.io as pio
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
import atexit
from translations import translations

logging.basicConfig(filename='app.log', level=logging.ERROR, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'NEscD7rN4cuYR3o3VLZZuSj3myhwAX7')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'ficore.ai.africa@gmail.com'
app.config['MAIL_PASSWORD'] = os.environ.get('SMTP_PASSWORD')
if not app.config['MAIL_PASSWORD']:
    logger.error("SMTP_PASSWORD environment variable not set")
mail = Mail(app)

scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
try:
    creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
except Exception as e:
    logger.error(f"Failed to initialize Google Sheets credentials: {e}")

try:
    spreadsheet = client.open_by_key('13hbiMTMRBHo9MHjWwcugngY_aSiuxII67HCf03MiZ8I')
except Exception as e:
    logger.error(f"Error accessing Google Sheets: {e}")

WORKSHEETS = {
    'HealthScore': {'name': 'HealthScoreSheet', 'headers': ['Timestamp', 'BusinessName', 'IncomeRevenue', 'ExpensesCosts', 'DebtLoan', 'DebtInterestRate', 'AutoEmail', 'PhoneNumber', 'FirstName', 'LastName', 'UserType', 'Email', 'Badges', 'Language', 'Score']},
    'NetWorth': {'name': 'NetWorthSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Assets', 'Liabilities', 'NetWorth']},
    'Quiz': {'name': 'QuizSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'QuizScore', 'Personality']},
    'EmergencyFund': {'name': 'EmergencyFundSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'MonthlyExpenses', 'RecommendedFund']},
    'Budget': {'name': 'BudgetSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'AutoEmail', 'Language', 'MonthlyIncome', 'HousingExpenses', 'FoodExpenses', 'TransportExpenses', 'OtherExpenses', 'TotalExpenses', 'Savings', 'SurplusDeficit']},
    'ExpenseTracker': {'name': 'ExpenseTrackerSheet', 'headers': ['ID', 'UserEmail', 'Amount', 'Category', 'Date', 'Description', 'Timestamp', 'TransactionType', 'RunningBalance']},
    'BillPlanner': {'name': 'BillPlannerSheet', 'headers': ['Timestamp', 'FirstName', 'Email', 'Language', 'Description', 'Amount', 'DueDate', 'Category', 'Recurrence', 'Status', 'SendEmail']}
}

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

def parse_number(value):
    try:
        if isinstance(value, str):
            value = value.replace(',', '')
        return float(value)
    except (ValueError, TypeError):
        return 0

def get_user_data_by_email(email, tool):
    try:
        records = sheets[tool].get_all_records()
        for record in records:
            if record.get('Email') == email or record.get('UserEmail') == email:
                return record
        return None
    except Exception as e:
        logger.error(f"Error fetching user data from {WORKSHEETS[tool]['name']}: {e}")
        return None

def get_record_by_id(id, tool):
    try:
        records = sheets[tool].get_all_records()
        for record in records:
            if record.get('ID') == id or record.get('Timestamp') == id:
                return record
        return None
    except Exception as e:
        logger.error(f"Error fetching record by ID from {WORKSHEETS[tool]['name']}: {e}")
        return None

def update_or_append_user_data(user_data, tool):
    language = session.get('language', 'English')
    sheet = sheets[tool]
    headers = WORKSHEETS[tool]['headers']
    try:
        records = sheet.get_all_records()
        email = user_data.get('Email') or user_data.get('UserEmail')
        id = user_data.get('ID') or user_data.get('Timestamp')
        if email:
            for i, record in enumerate(records, start=2):
                if record.get('Email') == email or record.get('UserEmail') == email or record.get('ID') == id or record.get('Timestamp') == id:
                    merged_data = {**record, **user_data}
                    sheet.update(f'A{i}:{chr(64 + len(headers))}{i}', [[merged_data.get(header, '') for header in headers]])
                    return
        sheet.append_row([user_data.get(header, '') for header in headers])
    except gspread.exceptions.APIError as e:
        logger.error(f"Google Sheets API error updating/appending data to {WORKSHEETS[tool]['name']}: {e}")
        flash(translations[language]['Failed to save data due to Google Sheets error'], 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])
    except Exception as e:
        logger.error(f"Error updating/appending data to {WORKSHEETS[tool]['name']}: {e}")
        flash(translations[language]['Failed to save data'], 'error')
        sheet.append_row([user_data.get(header, '') for header in headers])

def calculate_running_balance(email):
    try:
        records = sheets['ExpenseTracker'].get_all_records()
        balance = 0
        user_records = [r for r in records if r['UserEmail'] == email]
        for record in sorted(user_records, key=lambda x: x['Timestamp']):
            amount = float(record['Amount'])
            balance += amount if record['TransactionType'] == 'Income' else -amount
            record['RunningBalance'] = balance
            update_or_append_user_data(record, 'ExpenseTracker')
        return balance
    except Exception as e:
        logger.error(f"Error calculating running balance: {e}")
        return 0

def assign_net_worth_rank(net_worth):
    try:
        all_net_worths = [float(row['NetWorth']) for row in sheets['NetWorth'].get_all_records() if row['NetWorth'] and row['NetWorth'].strip()]
        all_net_worths.append(net_worth)
        rank_percentile = 100 - np.percentile(all_net_worths, np.searchsorted(sorted(all_net_worths, reverse=True), net_worth) / len(all_net_worths) * 100)
        return round(rank_percentile, 1)
    except Exception as e:
        logger.error(f"Error assigning net worth rank: {e}")
        return 50.0

def get_net_worth_advice(net_worth, language='English'):
    if net_worth > 0:
        return translations[language]['Maintain your positive net worth by continuing to manage liabilities and grow assets.']
    elif net_worth == 0:
        return translations[language]['Your net worth is balanced. Consider increasing assets to build wealth.']
    else:
        return translations[language]['Focus on reducing liabilities to improve your net worth.']

def assign_net_worth_badges(net_worth, language='English'):
    badges = []
    try:
        if net_worth > 0:
            badges.append(translations[language]['Positive Net Worth'])
        if net_worth >= 100000:
            badges.append(translations[language]['Wealth Builder'])
        if net_worth <= -50000:
            badges.append(translations[language]['Debt Recovery'])
    except Exception as e:
        logger.error(f"Error assigning net worth badges: {e}")
    return badges

def get_tips(language='English'):
    return [
        translations[language]['Regularly review your assets and liabilities to track progress.'],
        translations[language]['Invest in low-risk assets to grow your wealth steadily.'],
        translations[language]['Create a plan to pay down high-interest debt first.']
    ]

def get_courses(language='English'):
    return [
        {'title': translations[language]['Personal Finance 101'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': translations[language]['Debt Management Basics'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'},
        {'title': translations[language]['Investing for Beginners'], 'link': 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'}
    ]

def get_quiz_advice(score, personality, language='English'):
    if score >= 4:
        return translations[language]['Great job! Continue to leverage your {personality} approach to build wealth.'].format(personality=personality.lower())
    elif score >= 2:
        return translations[language]['Good effort! Your {personality} style is solid, but consider tracking expenses more closely.'].format(personality=personality.lower())
    else:
        return translations[language]['Keep learning! Your {personality} approach can improve with regular financial reviews.'].format(personality=personality.lower())

def assign_quiz_badges(score, language='English'):
    badges = []
    try:
        if score >= 4:
            badges.append(translations[language]['Financial Guru'])
        if score >= 2:
            badges.append(translations[language]['Quiz Achiever'])
        badges.append(translations[language]['Quiz Participant'])
    except Exception as e:
        logger.error(f"Error assigning quiz badges: {e}")
    return badges

def get_average_health_score():
    try:
        records = sheets['HealthScore'].get_all_records()
        scores = [float(row['Score']) for row in records if row['Score'] and row['Score'].strip()]
        return np.mean(scores) if scores else 50
    except Exception as e:
        logger.error(f"Error calculating average health score: {e}")
        return 50

def generate_health_score_charts(user_data, language='English'):
    try:
        income = user_data['IncomeRevenue']
        debt = user_data['DebtLoan']
        ratio_message = translations[language]['Your asset-to-liability ratio is healthy.'] if income >= 2 * debt else translations[language]['Your liabilities are high. Consider strategies to reduce debt.']
        bar_fig = go.Figure(data=[
            go.Bar(name=translations[language]['Income (₦)'], x=['Income'], y=[income], marker_color='#2E7D32'),
            go.Bar(name=translations[language]['Debt (₦)'], x=['Debt'], y=[debt], marker_color='#DC3545')
        ])
        bar_fig.update_layout(
            title=translations[language]['Asset-Liability Breakdown'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12),
            annotations=[dict(text=ratio_message, xref="paper", yref="paper", x=0.5, y=1.1, showarrow=False)]
        )
        chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=False)

        average_score = get_average_health_score()
        bar_fig = go.Figure(data=[
            go.Bar(name=translations[language]['Your Score'], x=['You'], y=[user_data['Score']], marker_color='#2E7D32'),
            go.Bar(name=translations[language]['Average Score'], x=['Average'], y=[average_score], marker_color='#0288D1')
        ])
        bar_fig.update_layout(
            title=translations[language]['Comparison to Peers'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        comparison_chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=False)

        return chart_html, comparison_chart_html
    except Exception as e:
        logger.error(f"Error generating health score charts: {e}")
        return "", ""

def generate_net_worth_charts(net_worth_data, language='English'):
    try:
        labels = [translations[language]['Assets'], translations[language]['Liabilities']]
        values = [net_worth_data['Assets'], net_worth_data['Liabilities']]
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545']))])
        pie_fig.update_layout(
            title=translations[language]['Asset-Liability Breakdown'],
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=False)

        all_net_worths = [float(row['NetWorth']) for row in sheets['NetWorth'].get_all_records() if row['NetWorth'] and row['NetWorth'].strip()]
        user_net_worth = net_worth_data['NetWorth']
        avg_net_worth = np.mean(all_net_worths) if all_net_worths else 0
        bar_fig = go.Figure(data=[
            go.Bar(name=translations[language]['Your Net Worth'], x=['You'], y=[user_net_worth], marker_color='#2E7D32'),
            go.Bar(name=translations[language]['Average Net Worth'], x=['Average'], y=[avg_net_worth], marker_color='#0288D1')
        ])
        bar_fig.update_layout(
            title=translations[language]['Comparison to Peers'],
            barmode='group',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        comparison_chart_html = pio.to_html(bar_fig, full_html=False, include_plotlyjs=False)

        return chart_html, comparison_chart_html
    except Exception as e:
        logger.error(f"Error generating net worth charts: {e}")
        return "", ""

def generate_budget_charts(budget_data, language='English'):
    try:
        labels = [
            translations[language]['Housing'],
            translations[language]['Food'],
            translations[language]['Transport'],
            translations[language]['Other'],
            translations[language]['Savings']
        ]
        values = [
            budget_data['HousingExpenses'],
            budget_data['FoodExpenses'],
            budget_data['TransportExpenses'],
            budget_data['OtherExpenses'],
            max(budget_data['Savings'], 0)
        ]
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545', '#0288D1', '#FFB300', '#4CAF50']))])
        pie_fig.update_layout(
            title=translations[language]['Budget Breakdown'],
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=False)
        return chart_html
    except Exception as e:
        logger.error(f"Error generating budget charts: {e}")
        return ""

def generate_expense_charts(email, language='English'):
    try:
        records = sheets['ExpenseTracker'].get_all_records()
        user_records = [r for r in records if r['UserEmail'] == email]
        categories = {}
        for record in user_records:
            if record['TransactionType'] == 'Expense':
                category = record['Category']
                amount = float(record['Amount'])
                categories[category] = categories.get(category, 0) + amount
        labels = list(categories.keys())
        values = list(categories.values())
        if not labels:
            return ""
        pie_fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.3, marker=dict(colors=['#2E7D32', '#DC3545', '#0288D1', '#FFB300', '#4CAF50', '#9C27B0']))])
        pie_fig.update_layout(
            title=translations[language]['Expense Breakdown by Category'],
            showlegend=True,
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(size=12)
        )
        chart_html = pio.to_html(pie_fig, full_html=False, include_plotlyjs=False)
        return chart_html
    except Exception as e:
        logger.error(f"Error generating expense charts: {e}")
        return ""

class UserForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    last_name = StringField('Last Name', validators=[Optional()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email()])
    phone = StringField('Phone Number', validators=[Optional()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    business_name = StringField('Business Name', validators=[DataRequired()])
    user_type = SelectField('User Type', choices=[('Individual', 'Individual'), ('Business', 'Business')], validators=[DataRequired()])
    income = FloatField('Monthly Income (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    expenses = FloatField('Monthly Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    debt = FloatField('Total Debt (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    interest_rate = FloatField('Debt Interest Rate (%)', validators=[Optional(), NumberRange(min=0, max=100)])
    auto_email = BooleanField('Send Email Notification', default=False)
    submit = SubmitField('Check My Financial Health')

class NetWorthForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    assets = FloatField('Total Assets (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    liabilities = FloatField('Total Liabilities (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    submit = SubmitField('Get My Net Worth')

class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    q1 = SelectField('Track Income/Expenses', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q2 = SelectField('Save vs Spend', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q3 = SelectField('Financial Risks', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q4 = SelectField('Emergency Fund', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q5 = SelectField('Review Goals', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    submit = SubmitField('Submit Quiz')

class EmergencyFundForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    monthly_expenses = FloatField('Monthly Essential Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    submit = SubmitField('Calculate Emergency Fund')

class BudgetForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    income = FloatField('Total Monthly Income (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    housing = FloatField('Housing Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    food = FloatField('Food Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    transport = FloatField('Transport Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    other = FloatField('Other Expenses (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    auto_email = BooleanField('Send Email Notification', default=False)
    submit = SubmitField('Plan My Budget')

class ExpenseForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    amount = FloatField('Amount (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    description = TextAreaField('Description', validators=[DataRequired()])
    category = SelectField('Category', choices=[
        ('Food and Groceries', 'Food and Groceries'),
        ('Transport', 'Transport'),
        ('Housing', 'Housing'),
        ('Utilities', 'Utilities'),
        ('Entertainment', 'Entertainment'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    transaction_type = SelectField('Transaction Type', choices=[('Income', 'Income'), ('Expense', 'Expense')], validators=[DataRequired()])
    submit = SubmitField('Add Transaction')

class BillForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    description = TextAreaField('Description', validators=[DataRequired()])
    amount = FloatField('Amount (₦)', validators=[DataRequired(), NumberRange(min=0, max=10000000000)])
    due_date = StringField('Due Date', validators=[DataRequired()])
    category = SelectField('Category', choices=[
        ('Utilities', 'Utilities'),
        ('Housing', 'Housing'),
        ('Transport', 'Transport'),
        ('Food', 'Food'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    recurrence = SelectField('Recurrence', choices=[
        ('None', 'None'),
        ('Daily', 'Daily'),
        ('Weekly', 'Weekly'),
        ('Monthly', 'Monthly'),
        ('Yearly', 'Yearly')
    ], validators=[DataRequired()])
    send_email = BooleanField('Send Email Notification', default=False)
    submit = SubmitField('Add Bill')

if APSCHEDULER_AVAILABLE:
    scheduler = BackgroundScheduler()
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

def schedule_bill_reminder(bill):
    try:
        due_date = parse(bill['DueDate'])
        reminder_date = due_date - timedelta(days=1)
        if reminder_date > datetime.now():
            scheduler.add_job(
                send_bill_reminder_email,
                'date',
                run_date=reminder_date,
                args=[bill],
                id=f"bill_reminder_{bill['Timestamp']}"
            )
    except Exception as e:
        logger.error(f"Error scheduling bill reminder: {e}")

def send_bill_reminder_email(bill):
    language = bill.get('Language', 'English')
    try:
        msg = Message(
            translations[language]['Bill Reminder Subject'].format(description=bill['Description']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[bill['Email']]
        )
        msg.html = translations[language]['Bill Reminder Email Body'].format(
            user_name=bill['FirstName'],
            description=bill['Description'],
            amount=bill['Amount'],
            due_date=bill['DueDate'],
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending bill reminder: {e}")
    except SMTPException as e:
        logger.error(f"SMTP error sending bill reminder: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending bill reminder: {e}")

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

def get_score_description(score):
    language = session.get('language', 'English')
    if score >= 80:
        return translations[language]['Strong Financial Health']
    elif score >= 60:
        return translations[language]['Stable Finances']
    elif score >= 40:
        return translations[language]['Financial Strain']
    else:
        return translations[language]['Urgent Attention Needed']

def assign_rank(score):
    try:
        all_scores = [float(row['Score']) for row in sheets['HealthScore'].get_all_records() if row['Score'] and row['Score'].strip()]
        all_scores.append(score)
        sorted_scores = sorted(all_scores, reverse=True)
        rank = sorted_scores.index(score) + 1
        total_users = len(all_scores)
        return rank, total_users
    except Exception as e:
        logger.error(f"Error assigning rank: {e}")
        return 1, 1

def assign_badges(score, debt, income):
    language = session.get('language', 'English')
    badges = []
    try:
        if score >= 60:
            badges.append(translations[language]['Financial Stability Achieved!'])
        if debt == 0:
            badges.append(translations[language]['Debt Slayer!'])
        if income > 0:
            badges.append(translations[language]['First Health Score Completed!'])
        if score >= 80:
            badges.append(translations[language]['High Value Badge'])
        elif score >= 60:
            badges.append(translations[language]['Positive Value Badge'])
    except Exception as e:
        logger.error(f"Error assigning badges: {e}")
    return badges

@app.route('/', methods=['GET'])
def index():
    return redirect(url_for('landing'))

@app.route('/landing')
def landing():
    language = session.get('language', 'English')
    return render_template('landing.html', translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/change_language', methods=['POST'])
def change_language():
    language = request.form.get('language', 'English')
    if language in ['English', 'Hausa']:
        session['language'] = language
        flash(translations[language]['Language changed successfully'], 'success')
    else:
        flash(translations[session.get('language', 'English')]['Invalid language selection'], 'error')
    return redirect(request.referrer or url_for('landing'))

@app.route('/Health_Score_Form', methods=['GET', 'POST'])
def Health_Score_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'HealthScore') if email else None
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
        auto_email=form_data.get('AutoEmail', False) if form_data else False
    )
    if email:
        form.email.render_kw = {'readonly': True}
        form.confirm_email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if form.email.data != form.confirm_email.data:
                flash(translations[language]['Emails Do Not Match'], 'error')
                return render_template('health_score_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            income = parse_number(form.income.data)
            expenses = parse_number(form.expenses.data)
            debt = parse_number(form.debt.data)
            interest_rate = parse_number(form.interest_rate.data or 0)
            health_score = calculate_health_score(income, expenses, debt, interest_rate)
            score_description = get_score_description(health_score)
            rank, total_users = assign_rank(health_score)
            badges = assign_badges(health_score, debt, income)
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
            update_or_append_user_data(user_data, 'HealthScore')
            if form.auto_email.data:
                try:
                    msg = Message(
                        translations[language]['Score Report Subject'].format(user_name=form.first_name.data),
                        sender='ficore.ai.africa@gmail.com',
                        recipients=[form.email.data]
                    )
                    course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
                    course_title = translations[language]['Recommended Course']
                    msg.html = translations[language]['Email Body'].format(
                        user_name=form.first_name.data,
                        health_score=health_score,
                        score_description=score_description,
                        rank=rank,
                        total_users=total_users,
                        course_url=course_url,
                        course_title=course_title,
                        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
                    )
                    mail.send(msg)
                    flash(translations[language]['Email sent successfully'], 'success')
                except SMTPAuthenticationError as e:
                    logger.error(f"SMTP authentication error sending health score email: {e}")
                    flash(translations[language]['Failed to send email due to authentication issue'], 'error')
                except SMTPException as e:
                    logger.error(f"SMTP error sending health score email: {e}")
                    flash(translations[language]['Failed to send email due to server issue'], 'error')
                except Exception as e:
                    logger.error(f"Unexpected error sending health score email: {e}")
                    flash(translations[language]['Failed to send email'], 'error')
            session['user_data'] = user_data
            session['score_description'] = score_description
            session['badges'] = badges
            session['average_score'] = get_average_health_score()
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('health_score_dashboard') + '?success=true')
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('health_score_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/health_score_dashboard')
def health_score_dashboard():
    language = session.get('language', 'English')
    user_data = session.get('user_data')
    score_description = session.get('score_description')
    badges = session.get('badges', [])
    average_score = session.get('average_score', 50)
    tips = get_tips(language)
    courses = get_courses(language)
    if not user_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Health_Score_Form'))
    chart_html, comparison_chart_html = generate_health_score_charts(user_data, language)
    return render_template(
        'health_score_dashboard.html',
        user_data=user_data,
        score_description=score_description,
        badges=badges,
        average_score=average_score,
        tips=tips,
        courses=courses,
        chart_html=chart_html,
        comparison_chart_html=comparison_chart_html,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_health_score_email')
def send_health_score_email():
    language = session.get('language', 'English')
    user_data = session.get('user_data')
    score_description = session.get('score_description')
    if not user_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Health_Score_Form'))
    try:
        msg = Message(
            translations[language]['Score Report Subject'].format(user_name=user_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[user_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        rank, total_users = assign_rank(user_data['Score'])
        msg.html = translations[language]['Email Body'].format(
            user_name=user_data['FirstName'],
            health_score=user_data['Score'],
            score_description=score_description,
            rank=rank,
            total_users=total_users,
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending health score email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending health score email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending health score email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('health_score_dashboard'))

@app.route('/Net_Worth_Form', methods=['GET', 'POST'])
def Net_Worth_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'NetWorth') if email else None
    form = NetWorthForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        assets=form_data.get('Assets', 0) if form_data else 0,
        liabilities=form_data.get('Liabilities', 0) if form_data else 0
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('net_worth_form.html', form=form, net_worth=0, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            try:
                assets = parse_number(form.assets.data)
                liabilities = parse_number(form.liabilities.data)
                net_worth = assets - liabilities
                user_data = {
                    'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'FirstName': form.first_name.data,
                    'Email': form.email.data,
                    'Language': form.language.data,
                    'Assets': assets,
                    'Liabilities': liabilities,
                    'NetWorth': net_worth
                }
                update_or_append_user_data(user_data, 'NetWorth')
                session['net_worth_data'] = user_data
                flash(translations[language]['Submission Success'], 'success')
                return redirect(url_for('net_worth_dashboard'))
            except Exception as e:
                logger.error(f"Error calculating net worth: {e}")
                flash(translations[language]['Error calculating net worth'], 'error')
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('net_worth_form.html', form=form, net_worth=0, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/net_worth_dashboard')
def net_worth_dashboard():
    language = session.get('language', 'English')
    net_worth_data = session.get('net_worth_data')
    if not net_worth_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Net_Worth_Form'))
    rank = assign_net_worth_rank(net_worth_data['NetWorth'])
    advice = get_net_worth_advice(net_worth_data['NetWorth'], language)
    badges = assign_net_worth_badges(net_worth_data['NetWorth'], language)
    tips = get_tips(language)
    courses = get_courses(language)
    chart_html, comparison_chart_html = generate_net_worth_charts(net_worth_data, language)
    return render_template(
        'net_worth_dashboard.html',
        net_worth_data=net_worth_data,
        rank=rank,
        advice=advice,
        badges=badges,
        tips=tips,
        courses=courses,
        chart_html=chart_html,
        comparison_chart_html=comparison_chart_html,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_net_worth_email')
def send_net_worth_email():
    language = session.get('language', 'English')
    net_worth_data = session.get('net_worth_data')
    if not net_worth_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Net_Worth_Form'))
    try:
        msg = Message(
            translations[language]['Net Worth Report Subject'].format(user_name=net_worth_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[net_worth_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        rank = assign_net_worth_rank(net_worth_data['NetWorth'])
        msg.html = translations[language]['Net Worth Email Body'].format(
            user_name=net_worth_data['FirstName'],
            net_worth=net_worth_data['NetWorth'],
            assets=net_worth_data['Assets'],
            liabilities=net_worth_data['Liabilities'],
            rank=rank,
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending net worth email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending net worth email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending net worth email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('net_worth_dashboard'))

@app.route('/Quiz_Form', methods=['GET', 'POST'])
def Quiz_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'Quiz') if email else None
    form = QuizForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        q1=form_data.get('Q1') if form_data else None,
        q2=form_data.get('Q2') if form_data else None,
        q3=form_data.get('Q3') if form_data else None,
        q4=form_data.get('Q4') if form_data else None,
        q5=form_data.get('Q5') if form_data else None
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('quiz_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            score = sum(1 for q in [form.q1.data, form.q2.data, form.q3.data, form.q4.data, form.q5.data] if q == 'Yes')
            personality = 'Conservative' if score <= 1 else 'Balanced' if score <= 3 else 'Risk-Taker'
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
            update_or_append_user_data(user_data, 'Quiz')
            session['quiz_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('quiz_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('quiz_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/quiz_dashboard')
def quiz_dashboard():
    language = session.get('language', 'English')
    quiz_data = session.get('quiz_data')
    if not quiz_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Quiz_Form'))
    score = quiz_data['QuizScore']
    personality = quiz_data['Personality']
    advice = get_quiz_advice(score, personality, language)
    badges = assign_quiz_badges(score, language)
    tips = get_tips(language)
    courses = get_courses(language)
    return render_template(
        'quiz_dashboard.html',
        quiz_data=quiz_data,
        score=score,
        personality=personality,
        advice = advice,
        badges=badges,
        tips=tips,
        courses=courses,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_quiz_email')
def send_quiz_email():
    language = session.get('language', 'English')
    quiz_data = session.get('quiz_data')
    if not quiz_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Quiz_Form'))
    try:
        msg = Message(
            translations[language]['Quiz Report Subject'].format(user_name=quiz_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[quiz_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        msg.html = translations[language]['Quiz Email Body'].format(
            user_name=quiz_data['FirstName'],
            score=quiz_data['QuizScore'],
            personality=quiz_data['Personality'],
            advice=get_quiz_advice(quiz_data['QuizScore'], quiz_data['Personality'], language),
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending quiz email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending quiz email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending quiz email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('quiz_dashboard'))

@app.route('/Emergency_Fund_Form', methods=['GET', 'POST'])
def Emergency_Fund_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'EmergencyFund') if email else None
    form = EmergencyFundForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        monthly_expenses=form_data.get('MonthlyExpenses') if form_data else None
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('emergency_fund_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            monthly_expenses = parse_number(form.monthly_expenses.data)
            recommended_fund = monthly_expenses * 6
            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'Language': form.language.data,
                'MonthlyExpenses': monthly_expenses,
                'RecommendedFund': recommended_fund
            }
            update_or_append_user_data(user_data, 'EmergencyFund')
            session['emergency_fund_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('emergency_fund_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('emergency_fund_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/emergency_fund_dashboard')
def emergency_fund_dashboard():
    language = session.get('language', 'English')
    emergency_fund_data = session.get('emergency_fund_data')
    if not emergency_fund_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Emergency_Fund_Form'))
    tips = get_tips(language)
    courses = get_courses(language)
    return render_template(
        'emergency_fund_dashboard.html',
        emergency_fund_data=emergency_fund_data,
        tips=tips,
        courses=courses,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_emergency_fund_email')
def send_emergency_fund_email():
    language = session.get('language', 'English')
    emergency_fund_data = session.get('emergency_fund_data')
    if not emergency_fund_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Emergency_Fund_Form'))
    try:
        msg = Message(
            translations[language]['Emergency Fund Report Subject'].format(user_name=emergency_fund_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[emergency_fund_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        msg.html = translations[language]['Emergency Fund Email Body'].format(
            user_name=emergency_fund_data['FirstName'],
            monthly_expenses=emergency_fund_data['MonthlyExpenses'],
            recommended_fund=emergency_fund_data['RecommendedFund'],
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending emergency fund email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending emergency fund email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending emergency fund email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('emergency_fund_dashboard'))

@app.route('/Budget_Form', methods=['GET', 'POST'])
def Budget_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'Budget') if email else None
    form = BudgetForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        income=form_data.get('MonthlyIncome') if form_data else None,
        housing=form_data.get('HousingExpenses') if form_data else None,
        food=form_data.get('FoodExpenses') if form_data else None,
        transport=form_data.get('TransportExpenses') if form_data else None,
        other=form_data.get('OtherExpenses') if form_data else None,
        auto_email=form_data.get('AutoEmail', False) if form_data else False
    )
    if email:
        form.email.render_kw = {'readonly': True}
        form.confirm_email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if form.email.data != form.confirm_email.data:
                flash(translations[language]['Emails Do Not Match'], 'error')
                return render_template('budget_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            income = parse_number(form.income.data)
            housing = parse_number(form.housing.data)
            food = parse_number(form.food.data)
            transport = parse_number(form.transport.data)
            other = parse_number(form.other.data)
            total_expenses = housing + food + transport + other
            savings = income * 0.2
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
            update_or_append_user_data(user_data, 'Budget')
            if form.auto_email.data:
                try:
                    msg = Message(
                        translations[language]['Budget Report Subject'].format(user_name=form.first_name.data),
                        sender='ficore.ai.africa@gmail.com',
                        recipients=[form.email.data]
                    )
                    course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
                    course_title = translations[language]['Recommended Course']
                    msg.html = translations[language]['Budget Email Body'].format(
                        user_name=form.first_name.data,
                        income=income,
                        total_expenses=total_expenses,
                        savings=savings,
                        surplus_deficit=surplus_deficit,
                        course_url=course_url,
                        course_title=course_title,
                        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
                        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
                        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
                    )
                    mail.send(msg)
                    flash(translations[language]['Email sent successfully'], 'success')
                except SMTPAuthenticationError as e:
                    logger.error(f"SMTP authentication error sending budget email: {e}")
                    flash(translations[language]['Failed to send email due to authentication issue'], 'error')
                except SMTPException as e:
                    logger.error(f"SMTP error sending budget email: {e}")
                    flash(translations[language]['Failed to send email due to server issue'], 'error')
                except Exception as e:
                    logger.error(f"Unexpected error sending budget email: {e}")
                    flash(translations[language]['Failed to send email'], 'error')
            session['budget_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('budget_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('budget_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/budget_dashboard')
def budget_dashboard():
    language = session.get('language', 'English')
    budget_data = session.get('budget_data')
    if not budget_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Budget_Form'))
    tips = get_tips(language)
    courses = get_courses(language)
    chart_html = generate_budget_charts(budget_data, language)
    return render_template(
        'budget_dashboard.html',
        budget_data=budget_data,
        tips=tips,
        courses=courses,
        chart_html=chart_html,
        translations=translations[language],
        language=language,
        FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
    )

@app.route('/send_budget_email')
def send_budget_email():
    language = session.get('language', 'English')
    budget_data = session.get('budget_data')
    if not budget_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Budget_Form'))
    try:
        msg = Message(
            translations[language]['Budget Report Subject'].format(user_name=budget_data['FirstName']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[budget_data['Email']]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        msg.html = translations[language]['Budget Email Body'].format(
            user_name=budget_data['FirstName'],
            income=budget_data['MonthlyIncome'],
            total_expenses=budget_data['TotalExpenses'],
            savings=budget_data['Savings'],
            surplus_deficit=budget_data['SurplusDeficit'],
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending budget email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending budget email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending budget email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('budget_dashboard'))

@app.route('/Expense_Tracker_Form', methods=['GET', 'POST'])
def Expense_Tracker_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'ExpenseTracker') if email else None
    form = ExpenseForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        amount=form_data.get('Amount') if form_data else None,
        description=form_data.get('Description') if form_data else None,
        category(form_data.get('Category') if form_data else None,
        transaction_type=form_data.get('TransactionType') if form_data else None
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('expense_tracker_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
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
                'RunningBalance': 0
            }
            update_or_append_user_data(user_data, 'ExpenseTracker')
            running_balance = calculate_running_balance(form.email.data)
            user_data['RunningBalance'] = running_balance
            session['expense_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('expense_tracker_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('expense_tracker_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/expense_tracker_dashboard')
def expense_tracker_dashboard():
    language = session.get('language', 'English')
    email = session.get('user_email')
    if not email:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Expense_Tracker_Form'))
    try:
        records = sheets['ExpenseTracker'].get_all_records()
        user_transactions = [r for r in records if r['UserEmail'] == email]
        running_balance = calculate_running_balance(email)
        chart_html = generate_expense_charts(email, language)
        tips = get_tips(language)
        courses = get_courses(language)
        return render_template(
            'expense_tracker_dashboard.html',
            transactions=user_transactions,
            running_balance=running_balance,
            chart_html=chart_html,
            tips=tips,
            courses=courses,
            translations=translations[language],
            language=language,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
    except Exception as e:
        logger.error(f"Error loading expense tracker dashboard: {e}")
        flash(translations[language]['Error loading transactions'], 'error')
        return redirect(url_for('Expense_Tracker_Form'))

@app.route('/send_expense_tracker_email')
def send_expense_tracker_email():
    language = session.get('language', 'English')
    email = session.get('user_email')
    if not email:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Expense_Tracker_Form'))
    try:
        records = sheets['ExpenseTracker'].get_all_records()
        user_transactions = [r for r in records if r['UserEmail'] == email]
        running_balance = calculate_running_balance(email)
        msg = Message(
            translations[language]['Expense Tracker Report Subject'].format(user_name=user_transactions[0]['FirstName'] if user_transactions else 'User'),
            sender='ficore.ai.africa@gmail.com',
            recipients=[email]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        transaction_summary = '\n'.join([f"{t['Date']} - {t['Description']}: ₦{t['Amount']} ({t['TransactionType']})" for t in user_transactions[-5:]])
        msg.html = translations[language]['Expense Tracker Email Body'].format(
            user_name=user_transactions[0]['FirstName'] if user_transactions else 'User',
            running_balance=running_balance,
            transaction_summary=transaction_summary,
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending expense tracker email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending expense tracker email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending expense tracker email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('expense_tracker_dashboard'))

@app.route('/Bill_Planner_Form', methods=['GET', 'POST'])
def Bill_Planner_Form():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email, 'BillPlanner') if email else None
    form = BillForm(
        first_name=form_data.get('FirstName') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language,
        description=form_data.get('Description') if form_data else None,
        amount=form_data.get('Amount') if form_data else None,
        due_date=form_data.get('DueDate') if form_data else None,
        category=form_data.get('Category') if form_data else None,
        recurrence=form_data.get('Recurrence') if form_data else None,
        send_email=form_data.get('SendEmail', False) if form_data else False
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('bill_planner_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            session['user_email'] = form.email.data
            session.permanent = True
            try:
                due_date = parse(form.due_date.data).strftime('%Y-%m-%d')
            except ValueError:
                flash(translations[language]['Invalid due date format'], 'error')
                return render_template('bill_planner_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            amount = parse_number(form.amount.data)
            user_data = {
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'FirstName': form.first_name.data,
                'Email': form.email.data,
                'Language': form.language.data,
                'Description': form.description.data,
                'Amount': amount,
                'DueDate': due_date,
                'Category': form.category.data,
                'Recurrence': form.recurrence.data,
                'Status': 'Pending',
                'SendEmail': str(form.send_email.data)
            }
            update_or_append_user_data(user_data, 'BillPlanner')
            if form.send_email.data and APSCHEDULER_AVAILABLE:
                schedule_bill_reminder(user_data)
            session['bill_data'] = user_data
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('bill_planner_dashboard'))
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('bill_planner_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/bill_planner_dashboard')
def bill_planner_dashboard():
    language = session.get('language', 'English')
    email = session.get('user_email')
    if not email:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Bill_Planner_Form'))
    try:
        records = sheets['BillPlanner'].get_all_records()
        user_bills = [r for r in records if r['Email'] == email]
        tips = get_tips(language)
        courses = get_courses(language)
        return render_template(
            'bill_planner_dashboard.html',
            bills=user_bills,
            tips=tips,
            courses=courses,
            translations=translations[language],
            language=language,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
    except Exception as e:
        logger.error(f"Error loading bill planner dashboard: {e}")
        flash(translations[language]['Error loading bills'], 'error')
        return redirect(url_for('Bill_Planner_Form'))

@app.route('/send_bill_planner_email')
def send_bill_planner_email():
    language = session.get('language', 'English')
    email = session.get('user_email')
    if not email:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('Bill_Planner_Form'))
    try:
        records = sheets['BillPlanner'].get_all_records()
        user_bills = [r for r in records if r['Email'] == email]
        msg = Message(
            translations[language]['Bill Planner Report Subject'].format(user_name=user_bills[0]['FirstName'] if user_bills else 'User'),
            sender='ficore.ai.africa@gmail.com',
            recipients=[email]
        )
        course_url = 'https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
        course_title = translations[language]['Recommended Course']
        bill_summary = '\n'.join([f"{b['DueDate']} - {b['Description']}: ₦{b['Amount']} ({b['Status']})" for b in user_bills[-5:]])
        msg.html = translations[language]['Bill Planner Email Body'].format(
            user_name=user_bills[0]['FirstName'] if user_bills else 'User',
            bill_summary=bill_summary,
            course_url=course_url,
            course_title=course_title,
            FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
            WAITLIST_FORM_URL='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
            CONSULTANCY_FORM_URL='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A'
        )
        mail.send(msg)
        flash(translations[language]['Email sent successfully'], 'success')
    except SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error sending bill planner email: {e}")
        flash(translations[language]['Failed to send email due to authentication issue'], 'error')
    except SMTPException as e:
        logger.error(f"SMTP error sending bill planner email: {e}")
        flash(translations[language]['Failed to send email due to server issue'], 'error')
    except Exception as e:
        logger.error(f"Unexpected error sending bill planner email: {e}")
        flash(translations[language]['Failed to send email'], 'error')
    return redirect(url_for('bill_planner_dashboard'))

@app.route('/update_bill_status/<bill_id>', methods=['POST'])
def update_bill_status(bill_id):
    language = session.get('language', 'English')
    email = session.get('user_email')
    if not email:
        flash(translations[language]['No user data available'], 'error')
        return redirect(url_for('Bill_Planner_Form'))
    try:
        bill = get_record_by_id(bill_id, 'BillPlanner')
        if not bill or bill['Email'] != email:
            flash(translations[language]['Bill not found or unauthorized'], 'error')
            return redirect(url_for('bill_planner_dashboard'))
        new_status = request.form.get('status', 'Pending')
        if new_status not in ['Pending', 'Paid', 'Overdue']:
            flash(translations[language]['Invalid status'], 'error')
            return redirect(url_for('bill_planner_dashboard'))
        bill['Status'] = new_status
        update_or_append_user_data(bill, 'BillPlanner')
        flash(translations[language]['Bill status updated'], 'success')
    except Exception as e:
        logger.error(f"Error updating bill status: {e}")
        flash(translations[language]['Error updating bill status'], 'error')
    return redirect(url_for('bill_planner_dashboard'))

if __name__ == '__main__':
    app.run(debug=True)
