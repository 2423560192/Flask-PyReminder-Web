import datetime
import hashlib
import jwt
import requests
from functools import wraps
from flask import request, redirect, url_for, flash, g
from app.config import get_config
from app.utils.db import get_db_session, close_db_session
from app.utils.redis_helper import redis_helper

config = get_config()

# 内存存储模式的任务列表（仅当Redis不可用时使用）
tasks = []


def parse_datetime(datetime_str):
    """解析日期时间字符串为datetime对象"""
    try:
        # 尝试解析ISO格式
        dt = datetime.datetime.fromisoformat(datetime_str)
        # 确保时区信息
        if dt.tzinfo is None:
            dt = config.TZ.localize(dt)
        return dt
    except ValueError:
        try:
            # 尝试解析常见格式
            formats = [
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%d-%m-%Y %H:%M",
                "%d/%m/%Y %H:%M"
            ]
            for fmt in formats:
                try:
                    dt = datetime.datetime.strptime(datetime_str, fmt)
                    # 添加时区信息
                    dt = config.TZ.localize(dt)
                    return dt
                except ValueError:
                    continue
            # 分别解析日期和时间
            if ' ' in datetime_str:
                date_str, time_str = datetime_str.split(' ', 1)
            else:
                # 假设只有日期
                date_str = datetime_str
                time_str = "00:00"

            # 解析日期
            for date_fmt in ["%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"]:
                try:
                    date_obj = datetime.datetime.strptime(date_str, date_fmt).date()
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"无法解析日期: {date_str}")

            # 解析时间
            for time_fmt in ["%H:%M", "%H:%M:%S"]:
                try:
                    time_obj = datetime.datetime.strptime(time_str, time_fmt).time()
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"无法解析时间: {time_str}")

            # 组合日期和时间
            dt = datetime.datetime.combine(date_obj, time_obj)
            # 添加时区信息
            dt = config.TZ.localize(dt)
            return dt
        except Exception as e:
            raise ValueError(f"无法解析日期时间: {datetime_str}，错误: {str(e)}")


# JWT相关函数
def generate_token(user_id, is_admin=False):
    """生成JWT令牌"""
    payload = {
        'exp': datetime.datetime.utcnow() + config.JWT_ACCESS_TOKEN_EXPIRES,
        'iat': datetime.datetime.utcnow(),
        'sub': user_id,
        'admin': is_admin
    }
    return jwt.encode(
        payload,
        config.JWT_SECRET_KEY,
        algorithm='HS256'
    )


def decode_token(token):
    """解码并验证JWT令牌"""
    try:
        payload = jwt.decode(
            token,
            config.JWT_SECRET_KEY,
            algorithms=['HS256']
        )
        return payload
    except jwt.ExpiredSignatureError:
        # 令牌已过期
        return None
    except jwt.InvalidTokenError:
        # 令牌无效
        return None


def get_token_from_request():
    """从请求中获取令牌"""
    # 从Cookie中获取
    token = request.cookies.get('token')
    if token:
        return token

    # 从Authorization头获取
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        return auth_header[7:]  # 移除'Bearer '前缀

    return None


def login_required(f):
    """登录验证装饰器"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = get_token_from_request()

        if not token:
            flash('请先登录', 'warning')
            return redirect(url_for('auth.login', next=request.url))

        payload = decode_token(token)
        if not payload:
            flash('登录已过期，请重新登录', 'warning')
            return redirect(url_for('auth.login', next=request.url))

        # 将用户信息添加到全局g对象
        g.user_id = payload['sub']
        g.is_admin = payload.get('admin', False)

        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    """管理员权限验证装饰器"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = get_token_from_request()

        if not token:
            flash('请先登录', 'warning')
            return redirect(url_for('auth.login', next=request.url))

        payload = decode_token(token)
        if not payload:
            flash('登录已过期，请重新登录', 'warning')
            return redirect(url_for('auth.login', next=request.url))

        if not payload.get('admin', False):
            flash('需要管理员权限', 'danger')
            return redirect(url_for('main.index'))

        # 将用户信息添加到全局g对象
        g.user_id = payload['sub']
        g.is_admin = True

        return f(*args, **kwargs)

    return decorated_function


def send_notification(task_title, task_content, task_time, token_name="默认", tokens=None):
    """发送通知"""
    try:
        # 首先尝试获取指定的token
        if not tokens:
            try:
                # 如果没有提供token，尝试从Token类获取
                from app.models.token import Token
                token = Token.get_token(token_name)
                if token:
                    # 不需要刷新tokens，直接使用找到的token
                    pass
                else:
                    # 如果找不到指定的token，尝试使用默认token
                    token = Token.get_token("默认")
                    print(f"警告: 通知账号'{token_name}'不存在，将使用默认通知账号")
            except Exception as e:
                print(f"获取token时出错: {str(e)}")
                # 使用配置的默认token
                token = config.DEFAULT_NOTIFICATION_TOKEN
        else:
            # 使用传入的tokens字典
            token = tokens.get(token_name)
            if not token:
                print(f"警告: 通知账号'{token_name}'不存在，将使用默认通知账号")
                token = tokens.get("默认", config.DEFAULT_NOTIFICATION_TOKEN)

        # 如果所有尝试都失败，无法发送通知
        if not token:
            print("错误: 无法找到有效的通知账号，无法发送通知")
            return False

        params = {
            'title': task_title,
            'content': f"""任务名称: {task_title}\n
任务内容: {task_content}\n
提醒时间: {task_time}\n

您设置的任务时间已到！请及时处理。
"""
        }

        print(f"正在发送通知: {token[:8]}*** URL={f'https://xizhi.qqoq.net/{token}.send'}")

        try:
            # 设置较长的超时时间和重试次数
            response = requests.get(
                f'https://xizhi.qqoq.net/{token}.send',
                params=params,
                verify=False,
                timeout=30  # 增加请求超时时间
            )

            print(f"通知请求状态码: {response.status_code}")
            if response.status_code == 200:
                response_text = response.text[:100] if len(response.text) > 100 else response.text
                print(f"息知API响应: {response_text}")
                print(f"已成功发送任务提醒通知：{task_title}，使用token: {token_name}")
                return True
            else:
                print(f"发送通知失败，状态码：{response.status_code}，响应内容：{response.text[:200]}")
                return False
        except requests.exceptions.Timeout:
            print(f"发送通知超时，可能是网络跨境问题，任务标题: {task_title}")
            return False
        except requests.exceptions.ConnectionError:
            print(f"发送通知连接错误，可能是网络限制问题，任务标题: {task_title}")
            return False

    except Exception as e:
        print(f"发送通知失败，详细错误: {str(e)}")
        import traceback
        traceback.print_exc()  # 打印详细堆栈信息
        return False
