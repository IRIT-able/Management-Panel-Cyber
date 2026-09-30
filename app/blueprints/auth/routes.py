from flask import render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, current_user
from . import bp
from .forms import LoginForm
from ...models import User, Student
from ...security import verify_password
from ...extensions import limiter
from datetime import datetime, timedelta
from flask import current_app
import logging
from ...security import get_client_ip

@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    """Unified login for admins/teachers (User) and students."""
    if current_user.is_authenticated:
        return redirect(url_for('admin.dashboard' if current_user.is_admin() else 'teacher.dashboard'))
    if session.get('student_id'):
        return redirect(url_for('student.dashboard'))

    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        password = form.password.data.strip()
        ip = get_client_ip(request)
        auth_logger = logging.getLogger('auth')

        # Try staff user first
        user = User.query.filter_by(email=username).first()
        if user:
            if user.locked_until and user.locked_until > datetime.utcnow():
                auth_logger.warning('user.locked ip=%s username=%s', ip, username)
                flash('Account temporarily locked due to repeated failed logins. Try again later.', 'danger')
                return render_template('auth/login.html', form=form)
            if user.is_active and verify_password(user.password_hash, password):
                user.failed_login_attempts = 0
                user.locked_until = None
                from ...extensions import db
                db.session.add(user)
                db.session.commit()
                auth_logger.info('user.success ip=%s user_id=%s username=%s', ip, user.id, user.email)
                login_user(user, remember=form.remember.data)
                flash(f'Welcome back, {user.email}!', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('admin.dashboard' if user.is_admin() else 'teacher.dashboard'))
            # Failure path for user
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            max_attempts = current_app.config.get('LOGIN_MAX_ATTEMPTS', 5)
            lock_minutes = current_app.config.get('LOGIN_LOCK_MINUTES', 15)
            if user.failed_login_attempts >= max_attempts:
                user.locked_until = datetime.utcnow() + timedelta(minutes=lock_minutes)
            from ...extensions import db
            db.session.add(user)
            db.session.commit()
            auth_logger.warning('user.failure ip=%s username=%s', ip, username)
            flash('Invalid username or password', 'danger')
            return render_template('auth/login.html', form=form)

        # Try student account
        student = Student.query.filter_by(username=username).first()
        if student:
            if student.locked_until and student.locked_until > datetime.utcnow():
                auth_logger.warning('student.locked ip=%s username=%s', ip, username)
                flash('Account temporarily locked due to repeated failed logins. Try again later.', 'danger')
                return render_template('auth/login.html', form=form)
            if student.is_active and student.check_password(password):
                student.failed_login_attempts = 0
                student.locked_until = None
                from ...extensions import db
                db.session.add(student)
                db.session.commit()
                session['student_id'] = student.id
                session['student_name'] = student.name
                auth_logger.info('student.success ip=%s student_id=%s username=%s', ip, student.id, username)
                flash(f'Welcome, {student.name}!', 'success')
                return redirect(url_for('student.dashboard'))
            # Failure path for student
            student.failed_login_attempts = (student.failed_login_attempts or 0) + 1
            max_attempts = current_app.config.get('LOGIN_MAX_ATTEMPTS', 5)
            lock_minutes = current_app.config.get('LOGIN_LOCK_MINUTES', 15)
            if student.failed_login_attempts >= max_attempts:
                student.locked_until = datetime.utcnow() + timedelta(minutes=lock_minutes)
            from ...extensions import db
            db.session.add(student)
            db.session.commit()
            auth_logger.warning('student.failure ip=%s username=%s', ip, username)
            flash('Invalid username or password', 'danger')
            return render_template('auth/login.html', form=form)

        # Unknown username
        logging.getLogger('auth').warning('login.unknown ip=%s username=%s', ip, username)
        flash('Invalid username or password', 'danger')
    return render_template('auth/login.html', form=form)


# Backward-compatible redirects
@bp.route('/teacher/login', methods=['GET'])
def teacher_login():
    return redirect(url_for('auth.login'))


@bp.route('/student/login', methods=['GET'])
def student_login():
    return redirect(url_for('auth.login'))


@bp.route('/teacher/logout')
def teacher_logout():
    """Logout route for teachers/admins"""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@bp.route('/student/logout')
def student_logout():
    """Logout route for students"""
    session.pop('student_id', None)
    session.pop('student_name', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))
