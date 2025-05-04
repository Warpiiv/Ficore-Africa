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
    'Score', 'Rank', 'Total Users'
]

# Check and set headers if they don't exist or are incorrect
try:
    current_headers = sheet.row_values(1)
    if not current_headers or current_headers != EXPECTED_HEADERS:
        sheet.clear()  # Clear the sheet if headers are incorrect
        sheet.append_row(EXPECTED_HEADERS)  # Set the correct headers
except Exception as e:
    print(f"Error setting headers: {e}")
    sheet.clear()
    sheet.append_row(EXPECTED_HEADERS)

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
    assets = FloatField('Total Assets', validators=[DataRequired(), NumberRange(min=0)])
    liabilities = FloatField('Total Liabilities', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Submit')

# Form for Financial Personality Quiz
class QuizForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired()])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    language = SelectField('Language', choices=[('English', 'English'), ('Hausa', 'Hausa')], validators=[DataRequired()])
    q1 = SelectField('Track Income/Expenses', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q2 = SelectField('Save vs Spend', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q3 = SelectField('Financial Risks', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q4 = SelectField('Emergency Fund', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    q5 = SelectField('Review Goals', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    submit = SubmitField('Submit')

# Form for Emergency Fund Calculator
class EmergencyFundForm(FlaskForm):
    monthly_expenses = FloatField('Monthly Essential Expenses', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Submit')

# Form for Monthly Budget Planner
class BudgetForm(FlaskForm):
    income = FloatField('Total Monthly Income', validators=[DataRequired(), NumberRange(min=0)])
    housing = FloatField('Housing Expenses', validators=[DataRequired(), NumberRange(min=0)])
    food = FloatField('Food Expenses', validators=[DataRequired(), NumberRange(min=0)])
    transport = FloatField('Transport Expenses', validators=[DataRequired(), NumberRange(min=0)])
    other = FloatField('Other Expenses', validators=[DataRequired(), NumberRange(min=0)])
    email = EmailField('Email', validators=[DataRequired(), Email()])
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
        score -= min(expense_ratio * 40, 40)  # Cap expense impact
        score -= min(debt_ratio * 30, 30)    # Cap debt impact
        if interest_rate:
            score -= min(interest_rate * 0.5, 20)  # Cap interest impact
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

@app.route('/', methods=['GET'])
def index():
    return redirect(url_for('landing'))

@app.route('/landing')
def landing():
    language = session.get('language', 'English')
    return render_template('landing.html', translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/submit', methods=['GET', 'POST'])
def submit():
    form = UserForm()
    language = session.get('language', 'English')
    if form.validate_on_submit():
        if form.email.data != form.confirm_email.data:
            flash(translations[language]['Emails Do Not Match'], 'error')
            return render_template('health_score_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')
        session['language'] = form.language.data
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
            'Total Users': total_users
        }
        try:
            sheet.append_row(list(user_data.values()))
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
        except Exception as e:
            flash(translations[language]['Failed to send email or save data: {}'].format(str(e)), 'error')
        session['user_data'] = user_data
        session['badges'] = badges
        return redirect(url_for('health_score_dashboard') + '?success=true')
    return render_template('health_score_form.html', form=form, translations=translations[language], language=language, FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/net_worth', methods=['GET', 'POST'])
def net_worth():
    form = NetWorthForm()
    language = session.get('language', 'English')
    net_worth = None
    if form.validate_on_submit():
        try:
            net_worth = form.assets.data - form.liabilities.data
            flash(translations[language]['Submission Success'], 'success')
        except Exception as e:
            flash(translations[language]['Error calculating net worth: {}'].format(str(e)), 'error')
    return render_template('net_worth_form.html', form=form, net_worth=net_worth, translations=translations[language])

@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    form = QuizForm()
    language = session.get('language', 'English')
    result = None
    if form.validate_on_submit():
        try:
            score = sum(1 for q in [form.q1.data, form.q2.data, form.q3.data, form.q4.data, form.q5.data] if q == 'Yes')
            if score >= 4:
                result = "You're a financial pro!"
            elif score >= 2:
                result = "You're on the right track!"
            else:
                result = "Time to brush up on your financial skills!"
            flash(translations[language]['Submission Success'], 'success')
        except Exception as e:
            flash(translations[language]['Error processing quiz: {}'].format(str(e)), 'error')
    return render_template('quiz_form.html', form=form, result=result, translations=translations[language])

@app.route('/emergency_fund', methods=['GET', 'POST'])
def emergency_fund():
    form = EmergencyFundForm()
    language = session.get('language', 'English')
    recommended_fund = None
    if form.validate_on_submit():
        try:
            recommended_fund = form.monthly_expenses.data * 4.5  # Average of 3-6 months
            flash(translations[language]['Submission Success'], 'success')
        except Exception as e:
            flash(translations[language]['Error calculating fund: {}'].format(str(e)), 'error')
    return render_template('emergency_fund_form.html', form=form, recommended_fund=recommended_fund, translations=translations[language])

@app.route('/budget', methods=['GET', 'POST'])
def budget():
    form = BudgetForm()
    language = session.get('language', 'English')
    budget_summary = None
    if form.validate_on_submit():
        try:
            total_expenses = form.housing.data + form.food.data + form.transport.data + form.other.data
            savings = form.income.data - total_expenses
            budget_summary = {
                'income': form.income.data,
                'housing': form.housing.data,
                'food': form.food.data,
                'transport': form.transport.data,
                'other': form.other.data,
                'total_expenses': total_expenses,
                'savings': savings
            }
            flash(translations[language]['Submission Success'], 'success')
        except Exception as e:
            flash(translations[language]['Error calculating budget: {}'].format(str(e)), 'error')
    return render_template('budget_form.html', form=form, budget_summary=budget_summary, translations=translations[language])

@app.route('/expense_tracker', methods=['GET', 'POST'])
def expense_tracker():
    form = ExpenseForm()
    language = session.get('language', 'English')
    expenses = session.get('expenses', [])
    if form.validate_on_submit():
        try:
            expense = {
                'id': str(uuid.uuid4()),
                'amount': form.amount.data,
                'category': form.category.data,
                'date': form.date.data,
                'description': form.description.data
            }
            expenses.append(expense)
            session['expenses'] = expenses
            flash(translations[language]['Submission Success'], 'success')
        except Exception as e:
            flash(translations[language]['Error adding expense: {}'].format(str(e)), 'error')
        return redirect(url_for('expense_tracker'))
    return render_template('expense_tracker.html', form=form, expenses=expenses, translations=translations[language])

@app.route('/edit_expense/<expense_id>', methods=['GET', 'POST'])
def edit_expense(expense_id):
    language = session.get('language', 'English')
    expenses = session.get('expenses', [])
    expense = next((e for e in expenses if e['id'] == expense_id), None)
    if not expense:
        flash(translations[language]['Error processing form'], 'error')
        return redirect(url_for('expense_tracker'))
    form = ExpenseForm(data=expense)
    if form.validate_on_submit():
        try:
            expense.update({
                'amount': form.amount.data,
                'category': form.category.data,
                'date': form.date.data,
                'description': form.description.data
            })
            session['expenses'] = expenses
            flash(translations[language]['Submission Success'], 'success')
        except Exception as e:
            flash(translations[language]['Error updating expense: {}'].format(str(e)), 'error')
        return redirect(url_for('expense_tracker'))
    return render_template('expense_edit_form.html', form=form, expense_id=expense_id, translations=translations[language])

@app.route('/bill_planner', methods=['GET', 'POST'])
def bill_planner():
    form = BillForm()
    language = session.get('language', 'English')
    tasks = session.get('bills', [])
    current_date = datetime.now().date()

    # Update task status and check for email notifications
    for task in tasks:
        try:
            due_date = parse(task['DueDate']).date()
            if task['status'] != 'Completed' and due_date < current_date:
                task['status'] = 'Overdue'
            elif task['status'] != 'Completed' and due_date == current_date:
                task['status'] = 'Pending'
                if task.get('send_email', False) and APSCHEDULER_AVAILABLE:
                    send_bill_reminder(task)
            else:
                task['status'] = task.get('status', 'Pending')
        except ValueError as e:
            task['status'] = 'Pending'
            print(f"Date parsing error for task {task.get('id', 'unknown')}: {e}")

    if form.validate_on_submit():
        try:
            session['language'] = form.language.data
            task = {
                'id': str(uuid.uuid4()),
                'first_name': form.first_name.data,
                'email': form.email.data,
                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'Description': form.description.data,
                'Amount': form.amount.data,
                'DueDate': form.due_date.data,
                'Category': form.category.data,
                'Recurrence': form.recurrence.data,
                'status': 'Pending',
                'send_email': form.send_email.data
            }
            due_date = parse(form.due_date.data).date()
            if due_date < current_date:
                task['status'] = 'Overdue'
            tasks.append(task)
            session['bills'] = tasks
            flash(translations[language]['Submission Success'], 'success')
        except ValueError as e:
            flash(translations[language]['Invalid date format: {}'].format(str(e)), 'error')
        except Exception as e:
            flash(translations[language]['Error adding task: {}'].format(str(e)), 'error')
        return redirect(url_for('bill_planner'))

    return render_template('bill_planner.html', form=form, tasks=tasks, translations=translations[language], FEEDBACK_FORM_URL='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ')

@app.route('/bill_edit/<id>', methods=['GET', 'POST'])
def bill_edit(id):
    language = session.get('language', 'English')
    tasks = session.get('bills', [])
    try:
        index = int(id)
        task = tasks[index] if 0 <= index < len(tasks) else None
        if not task:
            raise IndexError
    except (IndexError, ValueError) as e:
        flash(translations[language]['Error processing form: {}'].format(str(e)), 'error')
        return redirect(url_for('bill_planner'))
    form = BillForm(data=task)
    if form.validate_on_submit():
        try:
            task.update({
                'first_name': form.first_name.data,
                'email': form.email.data,
                'Description': form.description.data,
                'Amount': form.amount.data,
                'DueDate': form.due_date.data,
                'Category': form.category.data,
                'Recurrence': form.recurrence.data,
                'send_email': form.send_email.data
            })
            current_date = datetime.now().date()
            due_date = parse(form.due_date.data).date()
            if task['status'] != 'Completed':
                task['status'] = 'Overdue' if due_date < current_date else 'Pending'
            session['bills'] = tasks
            flash(translations[language]['Submission Success'], 'success')
        except ValueError as e:
            flash(translations[language]['Invalid date format: {}'].format(str(e)), 'error')
        except Exception as e:
            flash(translations[language]['Error updating task: {}'].format(str(e)), 'error')
        return redirect(url_for('bill_planner'))
    return render_template('bill_edit_form.html', form=form, id=id, translations=translations[language])

@app.route('/bill_complete/<id>')
def bill_complete(id):
    language = session.get('language', 'English')
    tasks = session.get('bills', [])
    try:
        index = int(id)
        task = tasks[index] if 0 <= index < len(tasks) else None
        if not task:
            raise IndexError
        task['status'] = 'Completed'
        session['bills'] = tasks
        flash(translations[language]['Submission Success'], 'success')
    except (IndexError, ValueError) as e:
        flash(translations[language]['Error completing task: {}'].format(str(e)), 'error')
    return redirect(url_for('bill_planner'))

@app.route('/update_email_notifications', methods=['POST'])
def update_email_notifications():
    language = session.get('language', 'English')
    tasks = session.get('bills', [])
    try:
        task_updates = json.loads(request.form.get('task_updates', '{}'))
        for task_id, enable_email in task_updates.items():
            index = int(task_id)
            if 0 <= index < len(tasks):
                tasks[index]['send_email'] = bool(enable_email)
        session['bills'] = tasks
        flash(translations[language]['Email notification settings updated'], 'success')
    except json.JSONDecodeError as e:
        flash(translations[language]['Error parsing updates: {}'].format(str(e)), 'error')
    except Exception as e:
        flash(translations[language]['Error updating email settings: {}'].format(str(e)), 'error')
    return redirect(url_for('bill_planner'))

def send_bill_reminder(task):
    language = session.get('language', 'English')
    try:
        subject = translations[language]['Bill Reminder Subject'].format(description=task['Description'])
        body = translations[language]['Bill Reminder Body'].format(
            first_name=task['first_name'],
            description=task['Description'],
            amount=f"₦{task['Amount']:.2f}",
            due_date=task['DueDate']
        )
        msg = Message(subject, sender='ficore.ai.africa@gmail.com', recipients=[task['email']])
        msg.html = body
        mail.send(msg)
        print(f"Email sent to {task['email']} for {task['Description']}")
    except Exception as e:
        print(f"Failed to send email for task {task.get('id', 'unknown')}: {e}")
        flash(translations[language]['Failed to send reminder email: {}'].format(str(e)), 'error')

def check_bills():
    if not APSCHEDULER_AVAILABLE:
        print("APScheduler not available, skipping bill checks")
        return
    tasks = session.get('bills', [])
    current_date = datetime.now().date()
    for task in tasks:
        try:
            due_date = parse(task['DueDate']).date()
            if task.get('send_email', False) and task['status'] != 'Completed' and due_date <= current_date:
                send_bill_reminder(task)
                if task['Recurrence'] != 'None':
                    new_due_date = due_date + timedelta(days=1 if task['Recurrence'] == 'Daily' else
                                                      7 if task['Recurrence'] == 'Weekly' else
                                                      30 if task['Recurrence'] == 'Monthly' else
                                                      365 if task['Recurrence'] == 'Yearly' else 0)
                    task['DueDate'] = new_due_date.strftime('%Y-%m-%d')
                    task['status'] = 'Pending'
        except ValueError as e:
            print(f"Date parsing error in check_bills for task {task.get('id', 'unknown')}: {e}")
        except Exception as e:
            print(f"Error processing task {task.get('id', 'unknown')} in check_bills: {e}")

# Scheduler setup
if APSCHEDULER_AVAILABLE:
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_bills, 'cron', hour=8, minute=0)  # Runs daily at 8 AM
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

@app.route('/change_language', methods=['POST'])
def change_language():
    language = request.form.get('language')
    if language in ['English', 'Hausa']:
        session['language'] = language
    return redirect(url_for('landing'))

@app.route('/health_score_dashboard')
def health_score_dashboard():
    language = session.get('language', 'English')
    user_data = session.get('user_data', {})
    badges = session.get('badges', [])
    if not user_data:
        flash(translations[language]['No data available'], 'warning')
        return redirect(url_for('landing'))
    return render_template(
        'health_score_dashboard.html',
        user_data=user_data,
        badges=badges,
        translations=translations[language],
        feedback_url='https://forms.gle/1g1FVulyf7ZvvXr7G0q7hAKwbGJMxV4blpjBuqrSjKzQ',
        waitlist_url='https://forms.gle/17e0XYcp-z3hCl0I-j2JkHoKKJrp4PfgujsK8D7uqNxo',
        consultancy_url='https://forms.gle/1TKvlT7OTvNS70YNd8DaPpswvqd9y7hKydxKr07gpK9A',
        course_url='https://youtube.com/@ficore.africa?si=xRuw7Ozcqbfmveru'
    )

@app.route('/net_worth_dashboard')
def net_worth_dashboard():
    language = session.get('language', 'English')
    return render_template('net_worth_dashboard.html', translations=translations[language])

@app.route('/quiz_dashboard')
def quiz_dashboard():
    language = session.get('language', 'English')
    return render_template('quiz_dashboard.html', translations=translations[language])

@app.route('/emergency_fund_dashboard')
def emergency_fund_dashboard():
    language = session.get('language', 'English')
    return render_template('emergency_fund_dashboard.html', translations=translations[language])

@app.route('/budget_dashboard')
def budget_dashboard():
    language = session.get('language', 'English')
    flash(translations[language]['No dashboard available for budget'], 'warning')
    return redirect(url_for('landing'))

@app.route('/dashboard')
def dashboard():
    return redirect(url_for('health_score_dashboard'))

if __name__ == '__main__':
    app.run(debug=True)