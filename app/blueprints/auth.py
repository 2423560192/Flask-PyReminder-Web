from flask import Blueprint, render_template, request
from app.controllers.auth_controller import AuthController
from app.utils.helpers import login_required

# 创建认证蓝图
auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """用户登录"""
    if request.method == 'POST':
        response = AuthController.login()
        if response:
            return response
    return render_template('auth/login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """用户注册"""
    if request.method == 'POST':
        response = AuthController.register()
        if response:
            return response
    return render_template('auth/register.html')


@auth_bp.route('/logout')
@login_required
def logout():
    """用户登出"""
    return AuthController.logout()
