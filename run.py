"""
时间提醒助手启动文件
"""
import os
import atexit
from app import create_app
from app.utils.db import close_db_session

# 创建应用实例
app = create_app()

# 注册退出时的清理函数
atexit.register(close_db_session)

if __name__ == '__main__':
    # 设置调试模式
    debug = os.getenv('FLASK_DEBUG', 'True').lower() in ['true', '1', 'yes']
    
    # 启动应用
    app.run(host='0.0.0.0', port=5000, debug=debug) 