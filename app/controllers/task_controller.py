import threading
import time
from flask import request, redirect, url_for, flash, g, jsonify
from app.models.task import Task
from app.models.token import Token
from app.utils.helpers import send_notification
from app.config import get_config
from app.utils.db import get_db_session
from datetime import datetime

config = get_config()

class TaskController:
    """任务控制器"""
    
    @staticmethod
    def add_task():
        """添加任务"""
        # 获取当前用户信息
        username = g.user_id
        is_admin = g.is_admin

        # 获取用户可用的通知账号
        available_tokens = Token.get_user_tokens(username if not is_admin else None)
        print(f"用户 {username} 添加任务时可用的通知账号: {list(available_tokens.keys())}")

        title = request.form.get('title', '未命名任务')
        content = request.form.get('content', '')
        reminder_date = request.form.get('date', '')
        reminder_time = request.form.get('time', '')
        token_name = request.form.get('token_name', '默认')

        print(f"用户 {username} 正在添加任务，使用通知账号: {token_name}")

        # 检查是否有权使用此token
        if not is_admin and token_name not in available_tokens:
            print(f"用户 {username} 尝试使用未授权的通知账号: {token_name}")
            from app.utils.db import get_redis_client
            r = get_redis_client()
            if r:
                token_info_json = r.hget(f"{config.TOKENS_KEY}:info", token_name)
                if token_info_json:
                    try:
                        import json
                        info = json.loads(token_info_json)
                        token_owner = info.get('owner')
                        print(f"通知账号 {token_name} 的所有者是: {token_owner}")
                        if token_owner != username:
                            flash(f"您无权使用通知账号 '{token_name}'", "danger")
                            return redirect(url_for('main.index'))
                    except Exception as e:
                        print(f"验证通知账号所有者出错: {str(e)}")
                        flash(f"无法验证通知账号 '{token_name}' 所有者", "danger")
                        return redirect(url_for('main.index'))
                else:
                    print(f"通知账号 {token_name} 没有所有者信息")
                    flash(f"通知账号 '{token_name}' 不存在或您无权使用", "danger")
                    return redirect(url_for('main.index'))

        if not reminder_date or not reminder_time:
            flash("日期和时间不能为空", "danger")
            return redirect(url_for('main.index'))

        # 验证token名称是否存在
        if token_name not in available_tokens and token_name != '默认':
            flash(f"通知账号 '{token_name}' 不存在", "danger")
            return redirect(url_for('main.index'))

        try:
            # 创建新任务
            task_id = Task.get_next_task_id()
            datetime_str = f"{reminder_date} {reminder_time}"
            
            # 保存任务
            if Task.save_task(task_id, title, content, datetime_str, token_name, username):
                flash("任务添加成功！", "success")
            else:
                flash("添加任务失败", "danger")
            
            return redirect(url_for('main.index'))
        except ValueError as e:
            print(f"日期解析错误: {str(e)}")
            flash("日期格式错误，请使用YYYY-MM-DD HH:MM格式", "danger")
            return redirect(url_for('main.index'))
    
    @staticmethod
    def delete_task(task_id):
        """删除任务"""
        # 获取当前用户信息
        username = g.user_id
        is_admin = g.is_admin

        # 删除任务
        if Task.delete_task(task_id, None if is_admin else username):
            flash("任务已删除", "success")
        else:
            if not is_admin:
                flash("您只能删除自己创建的任务", "danger")
            else:
                flash("任务不存在", "warning")
        
        return redirect(url_for('main.index'))
    
    @staticmethod
    def get_tasks():
        """获取任务列表"""
        # 获取当前用户信息
        username = g.user_id
        is_admin = g.is_admin

        # 获取任务列表
        if is_admin:
            # 管理员可以看到所有任务
            all_tasks = Task.get_all_tasks()
        else:
            # 普通用户只能看到自己的任务
            all_tasks = Task.get_user_tasks(username)
            print('所有任务： ' , all_tasks)

        serializable_tasks = []

        for task in all_tasks:
            serializable_task = task.copy()
            serializable_task["datetime"] = task["datetime"].strftime("%Y-%m-%d %H:%M")
            serializable_tasks.append(serializable_task)

        return jsonify(serializable_tasks)

class TaskChecker:
    """任务检查器，运行在后台线程"""
    
    @staticmethod
    def start_checker():
        """启动任务检查线程"""
        check_thread = threading.Thread(target=TaskChecker.check_tasks, daemon=True)
        check_thread.start()
        print("已启动任务检查线程")
        return check_thread
    
    @staticmethod
    def check_tasks():
        """检查并触发到期的任务"""
        while True:
            try:
                # 获取数据库会话
                session = get_db_session()
                if not session:
                    print("无法获取数据库会话，等待重试...")
                    time.sleep(60)  # 等待1分钟后重试
                    continue
                    
                try:
                    # 获取当前时间
                    now = datetime.now(config.TZ)
                    
                    # 查询需要触发的任务
                    tasks_to_trigger = session.query(Task).filter(
                        Task.triggered == False,
                        Task.datetime <= now
                    ).all()
                    
                    # 处理每个到期的任务
                    for task in tasks_to_trigger:
                        try:
                            # 获取通知Token
                            token = Token.get_token(task.token_name)
                            if not token:
                                print(f"任务 {task.id} 的通知Token不存在")
                                continue
                                
                            # 发送通知
                            send_notification(
                                token,
                                task.title,
                                task.content or "提醒时间到了！"
                            )
                            
                            # 标记任务为已触发
                            task.triggered = True
                            session.commit()
                            
                        except Exception as e:
                            print(f"处理任务 {task.id} 时出错: {str(e)}")
                            session.rollback()
                            continue
                            
                finally:
                    # 确保会话被关闭
                    from app.utils.db import close_db_session
                    close_db_session(session)
                    
            except Exception as e:
                print(f"任务检查出错: {str(e)}")
                
            # 等待下一次检查
            time.sleep(60)  # 每分钟检查一次 