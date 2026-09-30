from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_talisman import Talisman
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_apscheduler import APScheduler
import os

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
talisman = Talisman()
scheduler = APScheduler()
# Limiter will be initialized with app config in the factory
limiter = Limiter(key_func=get_remote_address, default_limits=[])

login_manager.login_view = 'auth.teacher_login'
login_manager.login_message = 'Please log in to access this page.'
