from flask import request, redirect, url_for, flash, make_response
from app.models.user import User
from app.utils.helpers import generate_token

class AuthController:
    """认证控制器"""
    
    @staticmethod
    def login():
        """用户登录"""
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')

            # 验证用户凭据
            is_valid, is_admin = User.verify_user(username, password)

            if is_valid:
                # 生成JWT令牌
                token = generate_token(username, is_admin)

                # 设置重定向响应
                next_page = request.args.get('next', url_for('main.index'))
                response = make_response(redirect(next_page))

                # 将令牌设置为Cookie
                from app.config import get_config
                config = get_config()
                response.set_cookie(
                    'token',
                    token,
                    httponly=True,
                    max_age=config.JWT_ACCESS_TOKEN_EXPIRES.total_seconds()
                )

                flash(f'欢迎回来，{username}！', 'success')
                return response
            else:
                flash('用户名或密码错误', 'danger')
        return None
    
    @staticmethod
    def register():
        """用户注册"""
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')

            # 基本验证
            if not username or not password:
                flash('用户名和密码不能为空', 'danger')
                return redirect(url_for('auth.register'))

            if password != confirm_password:
                flash('两次输入的密码不一致', 'danger')
                return redirect(url_for('auth.register'))

            # 创建用户
            if User.create_user(username, password):
                flash('注册成功，请登录', 'success')
                return redirect(url_for('auth.login'))
            else:
                flash('用户名已存在或注册失败', 'danger')
                return redirect(url_for('auth.register'))
        return None
    
    @staticmethod
    def logout():
        """用户登出"""
        response = make_response(redirect(url_for('auth.login')))
        response.delete_cookie('token')
        flash('您已成功登出', 'info')
        return response 