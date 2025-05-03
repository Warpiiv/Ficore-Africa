import os
import uuid
import json
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, SelectField, TextAreaField, EmailField, SubmitField
from wtforms.validators import DataRequired, Email, Optional, NumberRange
from translations import translations
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dateutil.parser import parse
from dateutil import parser
import random

# Initialize Flask app with custom template and static folders
# Set template_folder to 'ficore_templates' to match repository structure
# Set static_folder to 'static' to point to the static assets directory
app = Flask(__name__, template_folder='ficore_templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'your-secret-key')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Constants
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
SPREADSHEET_ID = 'your-spreadsheet-id'
CREDENTIALS_FILE = 'credentials.json'
FEEDBACK_FORM_URL = 'https://forms.gle/your-feedback-form'
WAITLIST_FORM_URL = 'https://forms.gle/your-waitlist-form'
CONSULTANCY_FORM_URL = 'https://forms.gle/your-consultancy-form'
SHEET_NAMES = {
    'submissions': 'Submissions',
    'net_worth': 'NetWorth',
    'emergency_fund': 'EmergencyFund',
    'quiz': 'Quiz',
    'budget': 'Budget',
    'expense_tracker': 'ExpenseTracker',
    'bill_planner': 'BillPlanner'
}
PREDETERMINED_HEADERS = {
    'Submissions': ['ID', 'First Name', 'Last Name', 'Email', 'Phone Number', 'Language', 'Business Name', 'User Type', 'Income/Revenue', 'Expenses/Costs', 'Debt/Loan', 'Debt Interest Rate', 'Timestamp'],
    'NetWorth': ['ID', 'First Name', 'Email', 'Language', 'Assets', 'Liabilities', 'Net Worth', 'Timestamp'],
    'EmergencyFund': ['ID', 'First Name', 'Email', 'Language', 'Monthly Expenses', 'Recommended Fund', 'Timestamp'],
    'Quiz': ['ID', 'First Name', 'Email', 'Language', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Score', 'Personality Type', 'Timestamp'],
    'Budget': ['ID', 'First Name', 'Email', 'Language', 'Monthly Income', 'Housing Expenses', 'Food Expenses', 'Transport Expenses', 'Other Expenses', 'Savings', 'Timestamp'],
    'ExpenseTracker': ['ID', 'User Email', 'Amount', 'Category', 'Date', 'Description', 'Timestamp'],
    'BillPlanner': ['ID', 'User Email', 'Bill Name', 'Amount', 'Due Date', 'Status', 'Timestamp']
}
CATEGORIES = [
    ('Food and Groceries', 'Food and Groceries'),
    ('Transport', 'Transport'),
    ('Housing', 'Housing'),
    ('Utilities', 'Utilities'),
    ('Entertainment', 'Entertainment'),
    ('Other', 'Other')
]

# Google Sheets Setup
def get_sheets_client():
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, SCOPES)
    client = gspread.authorize(creds)
    return client

def ensure_sheet_and_headers(sheet_name, headers):
    client = get_sheets_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=len(headers))
        worksheet.append_row(headers)
    existing_headers = worksheet.row_values(1)
    if existing_headers != headers:
        worksheet.clear()
        worksheet.append_row(headers)
    return worksheet

# Forms
class SubmissionForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    last_name = StringField('Last Name', validators=[Optional()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    auto_email = EmailField('Confirm Email', validators=[DataRequired(), Email()])
    phone_number = StringField('Phone Number', validators=[Optional()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    business_name = StringField('Business Name', validators=[DataRequired()])
    user_type = SelectField('User Type', choices=[('Individual', 'Individual'), ('Business', 'Business')], validators=[DataRequired()])
    income_revenue = FloatField('Income/Revenue', validators=[DataRequired(), NumberRange(min=0)])
    expenses_costs = FloatField('Expenses/Costs', validators=[DataRequired(), NumberRange(min=0)])
    debt_loan = FloatField('Debt/Loan', validators=[DataRequired(), NumberRange(min=0)])
    debt_interest_rate = FloatField('Debt Interest Rate (%)', validators=[DataRequired(), NumberRange(min=0, max=100)])
    submit = SubmitField('Submit')

class NetWorthForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    assets = FloatField('Assets', validators=[DataRequired(), NumberRange(min=0)])
    liabilities = FloatField('Liabilities', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Get your net worth instantly!')

class EmergencyFundForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    monthly_expenses = FloatField('Monthly Expenses', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Calculate Your Recommended Fund Size')

class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    q1 = SelectField('Question 1', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q2 = SelectField('Question 2', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q3 = SelectField('Question 3', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q4 = SelectField('Question 4', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q5 = SelectField('Question 5', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    submit = SubmitField('Uncover Your Financial Style')

class BudgetForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    auto_email = EmailField('Confirm Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    monthly_income = FloatField('Monthly Income', validators=[DataRequired(), NumberRange(min=0)])
    housing_expenses = FloatField('Housing Expenses', validators=[DataRequired(), NumberRange(min=0)])
    food_expenses = FloatField('Food Expenses', validators=[DataRequired(), NumberRange(min=0)])
    transport_expenses = FloatField('Transport Expenses', validators=[DataRequired(), NumberRange(min=0)])
    other_expenses = FloatField('Other Expenses', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Start Planning Your Budget!')

class ExpenseForm(FlaskForm):
    amount = FloatField('Amount', validators=[DataRequired(), NumberRange(min=0)])
    category = SelectField('Category', choices=CATEGORIES, validators=[DataRequired()])
    date = StringField('Date', validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional()])
    submit = SubmitField('Submit Expense')

class BillForm(FlaskForm):
    bill_name = StringField('Bill Name', validators=[DataRequired()])
    amount = FloatField('Amount', validators=[DataRequired(), NumberRange(min=0)])
    due_date = StringField('Due Date', validators=[DataRequired()])
    status = SelectField('Status', choices=[('Pending', 'Pending'), ('Paid', 'Paid')], validators=[DataRequired()])
    submit = SubmitField('Submit Bill')

# Helper Functions
def calculate_health_score(form_data):
    income = form_data.get('income_revenue', 0)
    expenses = form_data.get('expenses_costs', 0)
    debt = form_data.get('debt_loan', 0)
    interest_rate = form_data.get('debt_interest_rate', 0)
    
    savings_ratio = (income - expenses) / income if income > 0 else 0
    debt_to_income = debt / income if income > 0 else 1
    score = 100 * (0.5 * savings_ratio - 0.3 * debt_to_income - 0.2 * (interest_rate / 100))
    return max(0, min(100, round(score)))

def get_score_description(score):
    if score >= 80:
        return translations['English']['Strong Financial Health']
    elif score >= 50:
        return translations['English']['Stable Finances']
    elif score >= 20:
        return translations['English']['Financial Strain']
    else:
        return translations['English']['Urgent Attention Needed']

def suggest_category(description):
    if not description:
        return 'Other'
    description = description.lower()
    if any(keyword in description for keyword in ['food', 'groceries', 'market']):
        return 'Food and Groceries'
    elif any(keyword in description for keyword in ['transport', 'fuel', 'bus', 'taxi']):
        return 'Transport'
    elif any(keyword in description for keyword in ['rent', 'mortgage', 'housing']):
        return 'Housing'
    elif any(keyword in description for keyword in ['electricity', 'water', 'internet']):
        return 'Utilities'
    elif any(keyword in description for keyword in ['movie', 'concert', 'entertainment']):
        return 'Entertainment'
    return 'Other'

def parse_natural_date(date_str):
    try:
        parsed_date = parse(date_str, fuzzy=True)
        return parsed_date.strftime('%Y-%m-%d')
    except ValueError:
        return datetime.now().strftime('%Y-%m-%d')

def calculate_running_balance(email):
    worksheet = ensure_sheet_and_headers(SHEET_NAMES['expense_tracker'], PREDETERMINED_HEADERS['ExpenseTracker'])
    records = worksheet.get_all_records()
    user_expenses = [r for r in records if r['User Email'] == email]
    user_expenses.sort(key=lambda x: datetime.strptime(x['Date'], '%Y-%m-%d'))
    balance = 0
    for expense in user_expenses:
        balance -= float(expense['Amount'])
        expense['Running Balance'] = balance
    return user_expenses, balance

def generate_insights(email):
    expenses, balance = calculate_running_balance(email)
    if not expenses:
        return []
    categories = {}
    for expense in expenses:
        cat = expense['Category']
        categories[cat] = categories.get(cat, 0) + float(expense['Amount'])
    total_spent = sum(categories.values())
    insights = []
    for cat, amount in categories.items():
        percentage = (amount / total_spent) * 100 if total_spent > 0 else 0
        if percentage > 30:
            insights.append(f"You spent {percentage:.1f}% of your expenses on {cat}. Consider reviewing this category for savings.")
    if balance < 0:
        insights.append("Your running balance is negative. Prioritize reducing expenses or increasing income.")
    return insights

# Routes
@app.route('/')
def index():
    language = session.get('language', 'English')
    return render_template('landing.html', language=language, translations=translations[language], FEEDBACK_FORM_URL=FEEDBACK_FORM_URL)

@app.route('/set_language', methods=['POST'])
def set_language():
    language = request.form.get('language', 'English')
    session['language'] = language
    return redirect(url_for('index'))

@app.route('/financial_health')
def financial_health():
    language = session.get('language', 'English')
    form = SubmissionForm()
    return render_template('index.html', form=form, language=language, translations=translations[language])

@app.route('/submit', methods=['POST'])
def submit():
    form = SubmissionForm()
    language = session.get('language', 'English')
    if form.validate_on_submit():
        if form.email.data != form.auto_email.data:
            flash(translations[language]['Emails Do Not Match'], 'error')
            return redirect(url_for('financial_health'))
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['submissions'], PREDETERMINED_HEADERS['Submissions'])
        submission_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = [
            submission_id,
            form.first_name.data,
            form.last_name.data,
            form.email.data,
            form.phone_number.data,
            form.language.data,
            form.business_name.data,
            form.user_type.data,
            form.income_revenue.data,
            form.expenses_costs.data,
            form.debt_loan.data,
            form.debt_interest_rate.data,
            timestamp
        ]
        worksheet.append_row(data)
        health_score = calculate_health_score(form.data)
        score_description = get_score_description(health_score)
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('dashboard', health_score=health_score, score_description=score_description))
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(error, 'error')
        return redirect(url_for('financial_health'))

@app.route('/dashboard')
def dashboard():
    language = session.get('language', 'English')
    health_score = request.args.get('health_score', type=int, default=0)
    score_description = request.args.get('score_description', '')
    return render_template('dashboard.html', health_score=health_score, score_description=score_description, language=language, translations=translations[language])

@app.route('/net_worth', methods=['GET', 'POST'])
def net_worth():
    language = session.get('language', 'English')
    form = NetWorthForm()
    if form.validate_on_submit():
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['net_worth'], PREDETERMINED_HEADERS['NetWorth'])
        net_worth = form.assets.data - form.liabilities.data
        submission_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = [
            submission_id,
            form.first_name.data,
            form.email.data,
            form.language.data,
            form.assets.data,
            form.liabilities.data,
            net_worth,
            timestamp
        ]
        worksheet.append_row(data)
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('dashboard'))
    return render_template('net_worth_form.html', form=form, language=language, translations=translations[language])

@app.route('/emergency_fund', methods=['GET', 'POST'])
def emergency_fund():
    language = session.get('language', 'English')
    form = EmergencyFundForm()
    if form.validate_on_submit():
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['emergency_fund'], PREDETERMINED_HEADERS['EmergencyFund'])
        recommended_fund = form.monthly_expenses.data * 6
        submission_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = [
            submission_id,
            form.first_name.data,
            form.email.data,
            form.language.data,
            form.monthly_expenses.data,
            recommended_fund,
            timestamp
        ]
        worksheet.append_row(data)
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('dashboard'))
    return render_template('emergency_fund_form.html', form=form, language=language, translations=translations[language])

@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    language = session.get('language', 'English')
    form = QuizForm()
    if form.validate_on_submit():
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['quiz'], PREDETERMINED_HEADERS['Quiz'])
        score = sum(1 for q in ['q1', 'q2', 'q3', 'q4', 'q5'] if form[q].data == 'Yes')
        personality_types = {
            5: 'Financial Guru',
            4: 'Prudent Planner',
            3: 'Balanced Budgeter',
            2: 'Casual Spender',
            1: 'Risky Rover',
            0: 'Free Spirit'
        }
        personality = personality_types.get(score, 'Balanced Budgeter')
        submission_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = [
            submission_id,
            form.first_name.data,
            form.email.data,
            form.language.data,
            form.q1.data,
            form.q2.data,
            form.q3.data,
            form.q4.data,
            form.q5.data,
            score,
            personality,
            timestamp
        ]
        worksheet.append_row(data)
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('dashboard'))
    return render_template('quiz_form.html', form=form, language=language, translations=translations[language])

@app.route('/budget', methods=['GET', 'POST'])
def budget():
    language = session.get('language', 'English')
    form = BudgetForm()
    if form.validate_on_submit():
        if form.email.data != form.auto_email.data:
            flash(translations[language]['Emails Do Not Match'], 'error')
            return redirect(url_for('budget'))
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['budget'], PREDETERMINED_HEADERS['Budget'])
        total_expenses = sum([form.housing_expenses.data, form.food_expenses.data, form.transport_expenses.data, form.other_expenses.data])
        savings = form.monthly_income.data - total_expenses
        submission_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = [
            submission_id,
            form.first_name.data,
            form.email.data,
            form.language.data,
            form.monthly_income.data,
            form.housing_expenses.data,
            form.food_expenses.data,
            form.transport_expenses.data,
            form.other_expenses.data,
            savings,
            timestamp
        ]
        worksheet.append_row(data)
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('dashboard'))
    return render_template('budget_form.html', form=form, language=language, translations=translations[language])

@app.route('/expense_tracker', methods=['GET', 'POST'])
def expense_tracker():
    language = session.get('language', 'English')
    form = ExpenseForm()
    user_email = session.get('user_email', '')
    
    if form.validate_on_submit():
        suggested_category = suggest_category(form.description.data)
        parsed_date = parse_natural_date(form.date.data)
        expense_id = str(uuid.uuid4())
        expense = {
            'ID': expense_id,
            'User Email': user_email,
            'Amount': form.amount.data,
            'Category': form.category.data,
            'Date': parsed_date,
            'Description': form.description.data or '',
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Cache in session
        if 'expenses' not in session:
            session['expenses'] = []
        session['expenses'].append(expense)
        session.modified = True
        
        # Save to Google Sheets
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['expense_tracker'], PREDETERMINED_HEADERS['ExpenseTracker'])
        worksheet.append_row(list(expense.values()))
        
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('expense_tracker'))
    
    # Retrieve expenses from session or Google Sheets
    expenses = session.get('expenses', [])
    if not expenses and user_email:
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['expense_tracker'], PREDETERMINED_HEADERS['ExpenseTracker'])
        records = worksheet.get_all_records()
        expenses = [r for r in records if r['User Email'] == user_email]
        session['expenses'] = expenses
        session.modified = True
    
    insights = generate_insights(user_email) if user_email else []
    expenses, balance = calculate_running_balance(user_email)
    
    return render_template('expense_tracker_form.html', form=form, expenses=expenses, balance=balance, insights=insights, language=language, translations=translations[language])

@app.route('/expense_submit', methods=['POST'])
def expense_submit():
    language = session.get('language', 'English')
    form = ExpenseForm()
    user_email = session.get('user_email', '')
    
    if form.validate_on_submit():
        suggested_category = suggest_category(form.description.data)
        parsed_date = parse_natural_date(form.date.data)
        expense_id = str(uuid.uuid4())
        expense = {
            'ID': expense_id,
            'User Email': user_email,
            'Amount': form.amount.data,
            'Category': form.category.data,
            'Date': parsed_date,
            'Description': form.description.data or '',
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Cache in session
        if 'expenses' not in session:
            session['expenses'] = []
        session['expenses'].append(expense)
        session.modified = True
        
        # Save to Google Sheets
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['expense_tracker'], PREDETERMINED_HEADERS['ExpenseTracker'])
        worksheet.append_row(list(expense.values()))
        
        flash(translations[language]['Submission Success'], 'success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(error, 'error')
    
    return redirect(url_for('expense_tracker'))

@app.route('/expense_edit/<id>', methods=['GET', 'POST'])
def expense_edit(id):
    language = session.get('language', 'English')
    form = ExpenseForm()
    user_email = session.get('user_email', '')
    
    worksheet = ensure_sheet_and_headers(SHEET_NAMES['expense_tracker'], PREDETERMINED_HEADERS['ExpenseTracker'])
    records = worksheet.get_all_records()
    expense = next((r for r in records if r['ID'] == id and r['User Email'] == user_email), None)
    
    if not expense:
        flash('Expense not found or unauthorized access.', 'error')
        return redirect(url_for('expense_tracker'))
    
    if request.method == 'GET':
        form.amount.data = float(expense['Amount'])
        form.category.data = expense['Category']
        form.date.data = expense['Date']
        form.description.data = expense['Description']
    
    if form.validate_on_submit():
        parsed_date = parse_natural_date(form.date.data)
        updated_expense = {
            'ID': id,
            'User Email': user_email,
            'Amount': form.amount.data,
            'Category': form.category.data,
            'Date': parsed_date,
            'Description': form.description.data or '',
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Update session cache
        expenses = session.get('expenses', [])
        for i, exp in enumerate(expenses):
            if exp['ID'] == id:
                expenses[i] = updated_expense
                session['expenses'] = expenses
                session.modified = True
                break
        
        # Update Google Sheets
        for row_idx, row in enumerate(records, start=2):
            if row['ID'] == id:
                worksheet.update(f'A{row_idx}:G{row_idx}', [list(updated_expense.values())])
                break
        
        flash('Expense updated successfully!', 'success')
        return redirect(url_for('expense_tracker'))
    
    return render_template('expense_edit_form.html', form=form, expense_id=id, language=language, translations=translations[language])

@app.route('/bill_planner', methods=['GET', 'POST'])
def bill_planner():
    language = session.get('language', 'English')
    form = BillForm()
    user_email = session.get('user_email', '')
    
    if form.validate_on_submit():
        parsed_due_date = parse_natural_date(form.due_date.data)
        bill_id = str(uuid.uuid4())
        bill = {
            'ID': bill_id,
            'User Email': user_email,
            'Bill Name': form.bill_name.data,
            'Amount': form.amount.data,
            'Due Date': parsed_due_date,
            'Status': form.status.data,
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['bill_planner'], PREDETERMINED_HEADERS['BillPlanner'])
        worksheet.append_row(list(bill.values()))
        
        flash(translations[language]['Submission Success'], 'success')
        return redirect(url_for('bill_planner'))
    
    worksheet = ensure_sheet_and_headers(SHEET_NAMES['bill_planner'], PREDETERMINED_HEADERS['BillPlanner'])
    records = worksheet.get_all_records()
    bills = [r for r in records if r['User Email'] == user_email]
    bills.sort(key=lambda x: datetime.strptime(x['Due Date'], '%Y-%m-%d'))
    
    return render_template('bill_planner_form.html', form=form, bills=bills, language=language, translations=translations[language])

@app.route('/bill_submit', methods=['POST'])
def bill_submit():
    language = session.get('language', 'English')
    form = BillForm()
    user_email = session.get('user_email', '')
    
    if form.validate_on_submit():
        parsed_due_date = parse_natural_date(form.due_date.data)
        bill_id = str(uuid.uuid4())
        bill = {
            'ID': bill_id,
            'User Email': user_email,
            'Bill Name': form.bill_name.data,
            'Amount': form.amount.data,
            'Due Date': parsed_due_date,
            'Status': form.status.data,
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        worksheet = ensure_sheet_and_headers(SHEET_NAMES['bill_planner'], PREDETERMINED_HEADERS['BillPlanner'])
        worksheet.append_row(list(bill.values()))
        
        flash(translations[language]['Submission Success'], 'success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(error, 'error')
    
    return redirect(url_for('bill_planner'))

@app.route('/bill_edit/<id>', methods=['GET', 'POST'])
def bill_edit(id):
    language = session.get('language', 'English')
    form = BillForm()
    user_email = session.get('user_email', '')
    
    worksheet = ensure_sheet_and_headers(SHEET_NAMES['bill_planner'], PREDETERMINED_HEADERS['BillPlanner'])
    records = worksheet.get_all_records()
    bill = next((r for r in records if r['ID'] == id and r['User Email'] == user_email), None)
    
    if not bill:
        flash('Bill not found or unauthorized access.', 'error')
        return redirect(url_for('bill_planner'))
    
    if request.method == 'GET':
        form.bill_name.data = bill['Bill Name']
        form.amount.data = float(bill['Amount'])
        form.due_date.data = bill['Due Date']
        form.status.data = bill['Status']
    
    if form.validate_on_submit():
        parsed_due_date = parse_natural_date(form.due_date.data)
        updated_bill = {
            'ID': id,
            'User Email': user_email,
            'Bill Name': form.bill_name.data,
            'Amount': form.amount.data,
            'Due Date': parsed_due_date,
            'Status': form.status.data,
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        for row_idx, row in enumerate(records, start=2):
            if row['ID'] == id:
                worksheet.update(f'A{row_idx}:G{row_idx}', [list(updated_bill.values())])
                break
        
        flash('Bill updated successfully!', 'success')
        return redirect(url_for('bill_planner'))
    
    return render_template('bill_edit_form.html', form=form, bill_id=id, language=language, translations=translations[language])

@app.route('/bill_complete/<id>', methods=['POST'])
def bill_complete(id):
    language = session.get('language', 'English')
    user_email = session.get('user_email', '')
    
    worksheet = ensure_sheet_and_headers(SHEET_NAMES['bill_planner'], PREDETERMINED_HEADERS['BillPlanner'])
    records = worksheet.get_all_records()
    bill = next((r for r in records if r['ID'] == id and r['User Email'] == user_email), None)
    
    if not bill:
        flash('Bill not found or unauthorized access.', 'error')
        return redirect(url_for('bill_planner'))
    
    bill['Status'] = 'Paid'
    bill['Timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    for row_idx, row in enumerate(records, start=2):
        if row['ID'] == id:
            worksheet.update(f'A{row_idx}:G{row_idx}', [list(bill.values())])
            break
    
    flash('Bill marked as paid!', 'success')
    return redirect(url_for('bill_planner'))

# Error Handling
@app.errorhandler(404)
def page_not_found(e):
    language = session.get('language', 'English')
    return render_template('404.html', language=language, translations=translations[language]), 404

@app.errorhandler(500)
def internal_server_error(e):
    language = session.get('language', 'English')
    flash(translations[language]['Error processing form'], 'error')
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
