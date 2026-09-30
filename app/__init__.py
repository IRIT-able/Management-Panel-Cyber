from flask import Flask, session
from flask_sock import Sock
from .extensions import db, migrate, login_manager, csrf
from .models import User
from .extensions import talisman, limiter
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import logging
from logging.handlers import RotatingFileHandler


def create_app(config_object='config.DevelopmentConfig'):
    """Flask application factory"""
    app = Flask(__name__, static_folder='static', template_folder='templates')
    app.config.from_object(config_object)
    # If we're behind a reverse proxy (nginx) make sure ProxyFix is used
    if app.config.get('BEHIND_PROXY', False):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    
    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    
    from .extensions import scheduler
    
    # Initialize APScheduler
    app.config['SCHEDULER_API_ENABLED'] = False
    scheduler.init_app(app)
    
    # Simple file lock to prevent multiple workers from running the scheduler
    try:
        import fcntl
        lock_file = open("/tmp/scheduler.lock", "w")
        fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scheduler.start()
        
        # Schedule the cron job for off-hours shutdown (e.g. 5 PM)
        from .tasks import scheduled_shutdown_all_vms
        scheduler.add_job(
            id='scheduled_shutdown_all_vms',
            func=scheduled_shutdown_all_vms,
            args=[app.app_context()],
            trigger='cron',
            hour=17,
            minute=0
        )
    except BlockingIOError:
        # Another worker has the lock
        pass
    # Initialize security-focused extensions
    talisman.init_app(
        app,
        content_security_policy=app.config.get('CSP'),
        force_https=app.config.get('SSL_REDIRECT', True),
        strict_transport_security=True,
        strict_transport_security_max_age=31536000,
        strict_transport_security_include_subdomains=True,
        frame_options='DENY',
        content_security_policy_nonce_in=['script-src', 'style-src']
    )
    # Configure limiter storage and defaults from config
    limiter.init_app(app)
    sock = Sock(app)

    # Basic logging setup if not configured by the host
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    # Dedicated auth logger writing to instance/logs/auth.log (rotating)
    try:
        logs_dir = os.path.join(app.instance_path, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        auth_log_path = os.path.join(logs_dir, 'auth.log')
        auth_handler = RotatingFileHandler(auth_log_path, maxBytes=1_000_000, backupCount=3)
        auth_handler.setLevel(logging.INFO)
        auth_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        auth_logger = logging.getLogger('auth')
        # Avoid duplicate handlers on reload
        if not any(isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', None) == auth_log_path for h in auth_logger.handlers):
            auth_logger.addHandler(auth_handler)
        auth_logger.setLevel(logging.INFO)
        auth_logger.propagate = True  # also send to root/journald

        # Truncate auth log to last 10,000 lines on startup to prevent unbounded growth
        try:
            if os.path.exists(auth_log_path):
                with open(auth_log_path, 'rb') as f:
                    lines = f.readlines()
                if len(lines) > 10000:
                    with open(auth_log_path, 'wb') as f:
                        f.writelines(lines[-10000:])
        except Exception as _e:
            logging.getLogger(__name__).warning('Failed to truncate auth log: %s', _e)
    except Exception as e:
        logging.getLogger(__name__).warning('Failed to initialize auth file logger: %s', e)
    
    # User loader for Flask-Login
    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))
    
    # Register blueprints
    from .blueprints.auth import bp as auth_bp
    from .blueprints.admin import bp as admin_bp
    from .blueprints.teacher import bp as teacher_bp
    from .blueprints.student import bp as student_bp
    from .blueprints.api import bp as api_bp
    
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(teacher_bp, url_prefix='/teacher')
    app.register_blueprint(student_bp, url_prefix='/student')
    app.register_blueprint(api_bp, url_prefix='/api')
    
    # Exempt API blueprint from CSRF protection (these are AJAX endpoints)
    csrf.exempt(api_bp)
    
    # Register WebSocket routes
    from .blueprints.vnc_proxy.routes import register_websocket_routes
    register_websocket_routes(sock)
    
    # Root route
    @app.route('/')
    def index():
        from flask import redirect, url_for
        from flask_login import current_user
        if current_user.is_authenticated:
            return redirect(url_for('admin.dashboard' if current_user.is_admin() else 'teacher.dashboard'))
        if session.get('student_id'):
            return redirect(url_for('student.dashboard'))
        return redirect(url_for('auth.login'))
    
    # Error handlers
    @app.errorhandler(403)
    def forbidden(e):
        from flask import render_template
        return render_template('403.html'), 403
    
    @app.errorhandler(404)
    def not_found(e):
        from flask import render_template
        return render_template('404.html'), 404
    
    @app.errorhandler(500)
    def server_error(e):
        from flask import render_template
        return render_template('500.html'), 500
    
    return app
