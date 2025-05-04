import os
import uuid
import json
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, TextAreaField, EmailField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Email, Optional, NumberRange
from flask_mail import Mail, Message
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dateutil.parser import parse
import random
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
import atexit
from translations import translations

# Initialize Flask app with custom template and static folders
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'NEscD7rN4cuYR3o3VLZZuSj3myhwAX7')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

# Flask-Mail configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'ficore.ai.africa@gmail.com'
app.config['MAIL_PASSWORD'] = os.environ.get('SMTP_PASSWORD', 'xxqnglkfbidniatln')
mail = Mail(app)

# Google Sheets setup
scope = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

# Open the spreadsheet and ensure the "UserData" worksheet exists
spreadsheet = client.open_by_key('13hbiMTMRBHo9MHjWwcugngY_aSiuxII67HCf03MiZ8I')
try:
    sheet = spreadsheet.worksheet('UserData')
except gspread.exceptions.WorksheetNotFound:
    sheet = spreadsheet.add_worksheet(title='UserData', rows=100, cols=20)

# Define expected headers
EXPECTED_HEADERS = [
    'ID', 'Timestamp', 'First Name', 'Last Name', 'Email', 'Phone', 'Language',
    'Business Name', 'User Type', 'Income', 'Expenses', 'Debt', 'Interest Rate',
    'Score', 'Rank', 'Total Users', 'Net Worth', 'Emergency Fund', 'Quiz Score', 'Budget Savings'
]

# Check and set headers if they don't exist or are incorrect
try:
    current_headers = sheet.row_values(1)
    if not current_headers or current_headers != EXPECTED_HEADERS:
        sheet.clear()
        sheet.append_row(EXPECTED_HEADERS)
except Exception as e:
    print(f"Error setting headers: {e}")
    sheet.clear()
    sheet.append_row(EXPECTED_HEADERS)

# Helper function to get user data from Google Sheet by email
def get_user_data_by_email(email):
    try:
        records = sheet.get_all_records()
        for record in records:
            if record.get('Email') == email:
                return record
        return None
    except Exception as e:
        print(f"Error fetching user data: {e}")
        return None

# Form for user input
class UserForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    last_name = StringField('Last Name', validators=[Optional()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    confirm_email = EmailField('Confirm Email', validators=[DataRequired(), Email()])
    phone = StringField('Phone Number', validators=[Optional()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    business_name = StringField('Business Name', validators=[DataRequired()])
    user_type = SelectField('User Type', choices=[('Individual', 'Individual'), ('Business', 'Business')], validators=[DataRequired()])
    income = FloatField('Monthly Income', validators=[DataRequired(), NumberRange(min=0)])
    expenses = FloatField('Monthly Expenses', validators=[DataRequired(), NumberRange(min=0)])
    debt = FloatField('Total Debt', validators=[DataRequired(), NumberRange(min=0)])
    interest_rate = FloatField('Debt Interest Rate (%)', validators=[Optional(), NumberRange(min=0)])
    submit = SubmitField('Submit')

# Form for Net Worth Calculator
class NetWorthForm(FlaskForm):
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    assets = FloatField('Total Assets', validators=[DataRequired(), NumberRange(min=0)])
    liabilities = FloatField('Total Liabilities', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Submit')

# Form for Financial Personality Quiz
class QuizForm(FlaskForm):
    email = EmailField('Email', validators=[DataRequired(), Email()])
    first_name = StringField('First Name', validators=[DataRequired()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    q1 = SelectField('Track Income/Expenses', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q2 = SelectField('Save vs Spend', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q3 = SelectField('Financial Risks', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q4 = SelectField('Emergency Fund', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q5 = SelectField('Review Goals', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    submit = SubmitField('Submit')

# Form for Emergency Fund Calculator
class EmergencyFundForm(FlaskForm):
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    monthly_expenses = FloatField('Monthly Essential Expenses', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Submit')

# Form for Monthly Budget Planner
class BudgetForm(FlaskForm):
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    income = FloatField('Total Monthly Income', validators=[DataRequired(), NumberRange(min=0)])
    housing = FloatField('Housing Expenses', validators=[DataRequired(), NumberRange(min=0)])
    food = FloatField('Food Expenses', validators=[DataRequired(), NumberRange(min=0)])
    transport = FloatField('Transport Expenses', validators=[DataRequired(), NumberRange(min=0)])
    other = FloatField('Other Expenses', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Submit')

# Form for Expense Tracker
class ExpenseForm(FlaskForm):
    amount = FloatField('Amount', validators=[DataRequired(), NumberRange(min=0)])
    category = SelectField('Category', choices=[
        ('Food and Groceries', 'Food and Groceries'),
        ('Transport', 'Transport'),
        ('Housing', 'Housing'),
        ('Utilities', 'Utilities'),
        ('Entertainment', 'Entertainment'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    date = StringField('Date', validators=[DataRequired()])
    description = TextAreaField('Description', validators=[DataRequired()])
    submit = SubmitField('Submit')

# Form for Bill Planner
class BillForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    description = TextAreaField('Description', validators=[DataRequired()])
    amount = FloatField('Amount', validators=[DataRequired(), NumberRange(min=0)])
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
    submit = SubmitField('Submit')

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
        print(f"Error calculating health score: {e}")
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
        all_scores = [float(row['Score']) for row in sheet.get_all_records() if row['Score'] and row['Score'].strip()]
        all_scores.append(score)
        sorted_scores = sorted(all_scores, reverse=True)
        rank = sorted_scores.index(score) + 1
        total_users = len(all_scores)
        return rank, total_users
    except Exception as e:
        print(f"Error assigning rank: {e}")
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
        print(f"Error assigning badges: {e}")
    return badges

def update_or_append_user_data(user_data):
    try:
        records = sheet.get_all_records()
        email = user_data.get('Email')
        if email:
            for i, record in enumerate(records, start=2):
                if record.get('Email') == email:
                    # Merge existing data with new data
                    existing_data = record
                    merged_data = {**existing_data, **user_data}
                    sheet.update(f'A{i}:{chr(64 + len(EXPECTED_HEADERS))}{i}', [list(merged_data.values())])
                    return
            # If no existing record, append new row
            sheet.append_row(list(user_data.values()))
    except Exception as e:
        print(f"Error updating/appending data: {e}")
        sheet.append_row(list(user_data.values()))

@app.route('/', methods=['GET'])
def index():
    return redirect(url_for('landing'))

@app.route('/landing')
def landing():
    language = session.get('language', 'English')
    return render_template('landing.html', translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/submit', methods=['GET', 'POST'])
def submit():
    language = session.get('language', 'English')
    # Check for existing data in session or Google Sheet
    email = session.get('user_email')
    form_data = get_user_data_by_email(email) if email else None
    form = UserForm(
        first_name=form_data.get('First Name') if form_data else None,
        last_name=form_data.get('Last Name') if form_data else None,
        email=email,
        phone=form_data.get('Phone') if form_data else None,
        language=form_data.get('Language') if form_data else language,
        business_name=form_data.get('Business Name') if form_data else None,
        user_type=form_data.get('User Type') if form_data else None,
        income=form_data.get('Income') if form_data else None,
        expenses=form_data.get('Expenses') if form_data else None,
        debt=form_data.get('Debt') if form_data else None,
        interest_rate=form_data.get('Interest Rate') if form_data else None
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
            health_score = calculate_health_score(form.income.data, form.expenses.data, form.debt.data, form.interest_rate.data or 0)
            score_description = get_score_description(health_score)
            rank, total_users = assign_rank(health_score)
            badges = assign_badges(health_score, form.debt.data, form.income.data)
            user_data = {
                'ID': str(uuid.uuid4()),
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'First Name': form.first_name.data,
                'Last Name': form.last_name.data or '',
                'Email': form.email.data,
                'Phone': form.phone.data or '',
                'Language': form.language.data,
                'Business Name': form.business_name.data,
                'User Type': form.user_type.data,
                'Income': form.income.data,
                'Expenses': form.expenses.data,
                'Debt': form.debt.data,
                'Interest Rate': form.interest_rate.data or 0,
                'Score': health_score,
                'Rank': rank,
                'Total Users': total_users,
                'Net Worth': 0,
                'Emergency Fund': 0,
                'Quiz Score': 0,
                'Budget Savings': 0
            }
            update_or_append_user_data(user_data)

            # Send email notification
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

            session['user_data'] = user_data
            session['score_description'] = score_description
            session['badges'] = badges
            flash(translations[language]['Submission Success'], 'success')
            return redirect(url_for('health_score_dashboard') + '?success=true')  # Added ?success=true
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('health_score_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/health_score_dashboard')
def health_score_dashboard():
    language = session.get('language', 'English')
    user_data = session.get('user_data')
    score_description = session.get('score_description')
    badges = session.get('badges', [])
    if not user_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('submit'))
    return render_template('health_score_dashboard.html', user_data=user_data, score_description=score_description, badges=badges, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/net_worth', methods=['GET', 'POST'])
def net_worth():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email) if email else None
    form = NetWorthForm(
        email=email,
        language=form_data.get('Language') if form_data else language,
        assets=form_data.get('Net Worth', 0) + (form_data.get('Debt', 0) if form_data else 0),  # Approximate assets
        liabilities=form_data.get('Debt', 0) if form_data else 0
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('net_worth_form.html', form=form, net_worth=0, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            try:
                net_worth = form.assets.data - form.liabilities.data
                user_data = session.get('user_data', {})
                user_data.update({
                    'Net Worth': net_worth,
                    'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'Email': form.email.data
                })
                update_or_append_user_data(user_data)
                session['net_worth_data'] = {
                    'email': form.email.data,
                    'assets': form.assets.data,
                    'liabilities': form.liabilities.data,
                    'net_worth': net_worth,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                return redirect(url_for('net_worth_dashboard'))
            except Exception:
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
        return redirect(url_for('net_worth'))
    return render_template('net_worth_dashboard.html', net_worth_data=net_worth_data, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email) if email else None
    form = QuizForm(
        email=email,
        first_name=form_data.get('First Name') if form_data else None,
        language=form_data.get('Language') if form_data else language
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('quiz_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            try:
                score = sum(1 for answer in [form.q1.data, form.q2.data, form.q3.data, form.q4.data, form.q5.data] if answer == 'Yes')
                personality = 'Conservative' if score <= 2 else 'Balanced' if score <= 4 else 'Risk-Taker'
                user_data = session.get('user_data', {})
                user_data.update({
                    'Quiz Score': score,
                    'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'Email': form.email.data
                })
                update_or_append_user_data(user_data)
                session['quiz_data'] = {
                    'email': form.email.data,
                    'first_name': form.first_name.data,
                    'score': score,
                    'personality': personality,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                return redirect(url_for('quiz_dashboard'))
            except Exception:
                flash(translations[language]['Error processing quiz'], 'error')
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('quiz_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/quiz_dashboard')
def quiz_dashboard():
    language = session.get('language', 'English')
    quiz_data = session.get('quiz_data')
    if not quiz_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('quiz'))
    return render_template('quiz_dashboard.html', quiz_data=quiz_data, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/emergency_fund', methods=['GET', 'POST'])
def emergency_fund():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email) if email else None
    form = EmergencyFundForm(
        email=email,
        language=form_data.get('Language') if form_data else language,
        monthly_expenses=form_data.get('Expenses', 0) if form_data else 0
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('emergency_fund_form.html', form=form, recommended_fund=0, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            try:
                recommended_fund = form.monthly_expenses.data * 6
                user_data = session.get('user_data', {})
                user_data.update({
                    'Emergency Fund': recommended_fund,
                    'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'Email': form.email.data
                })
                update_or_append_user_data(user_data)
                session['emergency_fund_data'] = {
                    'email': form.email.data,
                    'monthly_expenses': form.monthly_expenses.data,
                    'recommended_fund': recommended_fund,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                return redirect(url_for('emergency_fund_dashboard'))
            except Exception:
                flash(translations[language]['Error calculating emergency fund'], 'error')
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('emergency_fund_form.html', form=form, recommended_fund=0, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/emergency_fund_dashboard')
def emergency_fund_dashboard():
    language = session.get('language', 'English')
    emergency_fund_data = session.get('emergency_fund_data')
    if not emergency_fund_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('emergency_fund'))
    return render_template('emergency_fund_dashboard.html', emergency_fund_data=emergency_fund_data, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/budget', methods=['GET', 'POST'])
def budget():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email) if email else None
    form = BudgetForm(
        email=email,
        language=form_data.get('Language') if form_data else language,
        income=form_data.get('Income', 0) if form_data else 0,
        housing=0,
        food=0,
        transport=0,
        other=0
    )
    if email:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('budget_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            try:
                total_expenses = form.housing.data + form.food.data + form.transport.data + form.other.data
                savings = form.income.data - total_expenses
                user_data = session.get('user_data', {})
                user_data.update({
                    'Budget Savings': savings,
                    'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'Email': form.email.data
                })
                update_or_append_user_data(user_data)
                session['budget_data'] = {
                    'email': form.email.data,
                    'income': form.income.data,
                    'housing': form.housing.data,
                    'food': form.food.data,
                    'transport': form.transport.data,
                    'other': form.other.data,
                    'total_expenses': total_expenses,
                    'savings': savings,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                return redirect(url_for('budget_dashboard'))
            except Exception:
                flash(translations[language]['Error calculating budget'], 'error')
        else:
            flash(translations[language]['Please correct the errors in the form'], 'error')
    return render_template('budget_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/budget_dashboard')
def budget_dashboard():
    language = session.get('language', 'English')
    budget_data = session.get('budget_data')
    if not budget_data:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('budget'))
    return render_template('budget_dashboard.html', budget_data=budget_data, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/expense_tracker', methods=['GET', 'POST'])
def expense_tracker():
    form = ExpenseForm()
    language = session.get('language', 'English')
    expenses = session.get('expenses', [])
    if request.method == 'POST':
        if form.validate_on_submit():
            try:
                date_str = form.date.data
                parsed_date = parse(date_str)
                expense = {
                    'id': str(uuid.uuid4()),
                    'amount': form.amount.data,
                    'category': form.category.data,
                    'date': parsed_date.strftime('%Y-%m-%d'),
                    'description': form.description.data
                }
                expenses.append(expense)
                session['expenses'] = expenses
                session.permanent = True
                flash(translations[language]['Submission Success'], 'success')
                return redirect(url_for('expense_tracker'))
            except ValueError:
                flash(translations[language]['Invalid date format'], 'error')
            except Exception:
                flash(translations[language]['Error adding expense'], 'error')
    return render_template('expense_tracker.html', form=form, expenses=expenses, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/edit_expense/<expense_id>', methods=['GET', 'POST'])
def edit_expense(expense_id):
    language = session.get('language', 'English')
    expenses = session.get('expenses', [])
    expense = next((exp for exp in expenses if exp['id'] == expense_id), None)
    if not expense:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('expense_tracker'))
    form = ExpenseForm(
        amount=expense['amount'],
        category=expense['category'],
        date=expense['date'],
        description=expense['description']
    )
    if request.method == 'POST':
        if form.validate_on_submit():
            try:
                date_str = form.date.data
                parsed_date = parse(date_str)
                expense.update({
                    'amount': form.amount.data,
                    'category': form.category.data,
                    'date': parsed_date.strftime('%Y-%m-%d'),
                    'description': form.description.data
                })
                session['expenses'] = expenses
                session.permanent = True
                flash(translations[language]['Submission Success'], 'success')
                return redirect(url_for('expense_tracker'))
            except ValueError:
                flash(translations[language]['Invalid date format'], 'error')
            except Exception:
                flash(translations[language]['Error updating expense'], 'error')
    return render_template('edit_expense.html', form=form, expense_id=expense_id, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/bill_planner', methods=['GET', 'POST'])
def bill_planner():
    language = session.get('language', 'English')
    email = session.get('user_email')
    form_data = get_user_data_by_email(email) if email else None
    form = BillForm(
        first_name=form_data.get('First Name') if form_data else None,
        email=email,
        language=form_data.get('Language') if form_data else language
    )
    if email:
        form.email.render_kw = {'readonly': True}
    bills = session.get('bills', [])
    if request.method == 'POST':
        if form.validate_on_submit():
            if email and form.email.data != email:
                flash(translations[language]['Email must match previous submission'], 'error')
                return render_template('bill_planner.html', form=form, bills=bills, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
            session['language'] = form.language.data
            try:
                due_date = parse(form.due_date.data)
                current_date = datetime.now()
                status = 'Overdue' if due_date < current_date else 'Pending'
                bill = {
                    'id': str(uuid.uuid4()),
                    'first_name': form.first_name.data,
                    'email': form.email.data,
                    'description': form.description.data,
                    'amount': form.amount.data,
                    'due_date': due_date.strftime('%Y-%m-%d'),
                    'category': form.category.data,
                    'recurrence': form.recurrence.data,
                    'status': status,
                    'send_email': form.send_email.data
                }
                bills.append(bill)
                session['bills'] = bills
                session.permanent = True
                if form.send_email.data:
                    schedule_bill_reminder(bill)
                flash(translations[language]['Submission Success'], 'success')
                return redirect(url_for('bill_planner'))
            except ValueError:
                flash(translations[language]['Invalid date format'], 'error')
            except Exception:
                flash(translations[language]['Error adding task'], 'error')
    return render_template('bill_planner.html', form=form, bills=bills, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/edit_bill/<bill_id>', methods=['GET', 'POST'])
def edit_bill(bill_id):
    language = session.get('language', 'English')
    bills = session.get('bills', [])
    bill = next((b for b in bills if b['id'] == bill_id), None)
    if not bill:
        flash(translations[language]['No data available'], 'error')
        return redirect(url_for('bill_planner'))
    form = BillForm(
        first_name=bill['first_name'],
        email=bill['email'],
        language=bill.get('language', session.get('language', 'English')),
        description=bill['description'],
        amount=bill['amount'],
        due_date=bill['due_date'],
        category=bill['category'],
        recurrence=bill['recurrence'],
        send_email=bill['send_email']
    )
    if bill['email']:
        form.email.render_kw = {'readonly': True}
    if request.method == 'POST':
        if form.validate_on_submit():
            try:
                due_date = parse(form.due_date.data)
                current_date = datetime.now()
                status = 'Overdue' if due_date < current_date else 'Pending'
                bill.update({
                    'first_name': form.first_name.data,
                    'email': form.email.data,
                    'description': form.description.data,
                    'amount': form.amount.data,
                    'due_date': due_date.strftime('%Y-%m-%d'),
                    'category': form.category.data,
                    'recurrence': form.recurrence.data,
                    'status': status,
                    'send_email': form.send_email.data
                })
                session['bills'] = bills
                session.permanent = True
                if form.send_email.data:
                    schedule_bill_reminder(bill)
                flash(translations[language]['Submission Success'], 'success')
                return redirect(url_for('bill_planner'))
            except ValueError:
                flash(translations[language]['Invalid date format'], 'error')
            except Exception:
                flash(translations[language]['Error updating task'], 'error')
    return render_template('edit_bill.html', form=form, bill_id=bill_id, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/complete_bill/<bill_id>', methods=['POST'])
def complete_bill(bill_id):
    language = session.get('language', 'English')
    bills = session.get('bills', [])
    bill = next((b for b in bills if b['id'] == bill_id), None)
    if bill:
        try:
            bill['status'] = 'Completed'
            session['bills'] = bills
            session.permanent = True
            flash(translations[language]['Submission Success'], 'success')
        except Exception:
            flash(translations[language]['Error completing task'], 'error')
    else:
        flash(translations[language]['No data available'], 'error')
    return redirect(url_for('bill_planner'))

# Scheduler for bill reminders
if APSCHEDULER_AVAILABLE:
    scheduler = BackgroundScheduler()
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

def send_reminder_email(bill):
    language = session.get('language', 'English')
    try:
        msg = Message(
            translations[language]['Bill Reminder Subject'].format(description=bill['description']),
            sender='ficore.ai.africa@gmail.com',
            recipients=[bill['email']]
        )
        msg.html = translations[language]['Bill Reminder Body'].format(
            first_name=bill['first_name'],
            description=bill['description'],
            amount=bill['amount'],
            due_date=bill['due_date']
        )
        mail.send(msg)
    except Exception as e:
        print(f"Failed to send reminder email: {e}")

def schedule_bill_reminder(bill):
    if not APSCHEDULER_AVAILABLE:
        return
    due_date = parse(bill['due_date'])
    reminder_time = due_date - timedelta(days=1)
    if reminder_time > datetime.now():
        scheduler.add_job(
            func=send_reminder_email,
            trigger='date',
            run_date=reminder_time,
            args=[bill],
            id=f"reminder_{bill['id']}"
        )

@app.route('/change_language', methods=['POST'])
def change_language():
    language = request.form.get('language', 'English')
    session['language'] = language
    session.permanent = True
    return redirect(request.referrer or url_for('landing'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
