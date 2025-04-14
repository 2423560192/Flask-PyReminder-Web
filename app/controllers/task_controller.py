import threading
import time
from flask import request, redirect, url_for, flash, g, jsonify
from app.models.task import Task
from app.models.token import Token
from app.utils.helpers import send_notification
from app.config import get_config
from app.utils.db import get_db_session, close_db_session
from app.utils.redis_helper import redis_helper
from app.utils.constants import TASKS_KEY, PENDING_TASKS_KEY
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

        # 支持新的datetime字段和旧的date/time分开字段
        datetime_str = request.form.get('datetime')

        if datetime_str:
            # 使用新的合并后的日期时间字段
            reminder_date = None
            reminder_time = None
        else:
            # 向后兼容：使用分开的日期和时间字段
            reminder_date = request.form.get('date', '')
            reminder_time = request.form.get('time', '')
            datetime_str = f"{reminder_date} {reminder_time}" if reminder_date and reminder_time else ''

        token_name = request.form.get('token_name', '默认')

        print(f"用户 {username} 正在添加任务，使用通知账号: {token_name}, 提醒时间: {datetime_str}")

        # 检查是否有权使用此token
        if not is_admin and token_name not in available_tokens:
            print(f"用户 {username} 尝试使用未授权的通知账号: {token_name}")
            r = redis_helper.get_client()
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

        if not datetime_str:
            flash("提醒时间不能为空", "danger")
            return redirect(url_for('main.index'))

        # 验证token名称是否存在
        if token_name not in available_tokens and token_name != '默认':
            flash(f"通知账号 '{token_name}' 不存在", "danger")
            return redirect(url_for('main.index'))

        try:
            # 创建新任务
            task_id = Task.get_next_task_id()

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
            print('所有任务： ', all_tasks)

        serializable_tasks = []

        for task in all_tasks:
            serializable_task = task.copy()
            serializable_task["datetime"] = task["datetime"].strftime("%Y-%m-%d %H:%M")
            serializable_tasks.append(serializable_task)

        return jsonify(serializable_tasks)


class TaskChecker:
    """任务检查器，运行在后台线程"""
    
    _instance = None
    _check_thread = None
    _running = False

    @classmethod
    def start_checker(cls):
        """启动任务检查线程（单例模式）"""
        if cls._check_thread is None or not cls._check_thread.is_alive():
            cls._running = True
            cls._check_thread = threading.Thread(target=cls.check_tasks, daemon=True)
            cls._check_thread.start()
            print("已启动任务检查线程")
        return cls._check_thread

    @classmethod
    def stop_checker(cls):
        """停止任务检查线程"""
        cls._running = False
        print("任务检查线程将在下一次循环结束时停止")

    @classmethod
    def is_running(cls):
        """检查任务检查线程是否在运行"""
        return cls._running and cls._check_thread is not None and cls._check_thread.is_alive()

    @classmethod
    def check_tasks(cls):
        """检查并触发到期的任务"""
        # 导入Flask应用
        from app import create_app
        app = create_app()
        import json

        while cls._running:
            try:
                # 使用应用上下文
                with app.app_context():
                    # 会话将在需要时获取
                    session = None
                    
                    print("开始检查待触发的任务...")
                    
                    try:
                        # 使用Task模型的get_pending_tasks方法获取待触发的任务
                        tasks_to_trigger = Task.get_pending_tasks()
                        
                        if tasks_to_trigger:
                            print(f"找到 {len(tasks_to_trigger)} 个需要触发的任务")
                            
                            # 处理每个到期的任务
                            for task in tasks_to_trigger:
                                try:
                                    # 获取通知Token
                                    token_name = task.get('token_name')
                                    if not token_name:
                                        print(f"任务 {task.get('id')} 的通知Token名称为空")
                                        continue

                                    # 发送通知
                                    task_time_str = task.get('datetime').strftime("%Y-%m-%d %H:%M:%S") if task.get('datetime') else "未知时间"

                                    # 调用send_notification函数
                                    success = send_notification(
                                        task.get('title', '未命名任务'),
                                        task.get('content', '') or "提醒时间到了！",
                                        task_time_str,
                                        token_name
                                    )

                                    if success:
                                        print(f"成功发送任务 {task.get('id')} 的通知")
                                        
                                        # 使用统一的方法标记任务为已触发
                                        # 这个方法会更新Redis、数据库和内存中的状态
                                        Task.mark_task_triggered(task.get('id'))
                                        
                                    else:
                                        print(f"发送任务 {task.get('id')} 的通知失败")

                                except Exception as e:
                                    print(f"处理任务 {task.get('id')} 时出错: {str(e)}")
                                    import traceback
                                    traceback.print_exc()
                                    if session:
                                        try:
                                            session.rollback()
                                        except:
                                            pass
                                    continue
                        else:
                            print("没有找到需要触发的任务")
                            
                    except Exception as e:
                        print(f"获取待触发任务时出错: {str(e)}")
                        import traceback
                        traceback.print_exc()
                    
                    # 确保会话被关闭
                    if session:
                        close_db_session(session)

            except Exception as e:
                print(f"任务检查出错: {str(e)}")
                import traceback
                traceback.print_exc()

            # 等待下一次检查
            time.sleep(60)  # 每分钟检查一次
