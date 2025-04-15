"""
时间提醒助手启动文件
"""
import os
import atexit
import logging
from app import create_app
from app.utils.db import close_db_session

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('pyreminder')

# 创建应用实例
app = create_app()

# 全局清理函数
def cleanup_resources():
    """在应用退出时清理资源"""
    logger.info("应用正在关闭，清理资源...")
    try:
        # 确保在应用上下文中关闭数据库会话
        with app.app_context():
            close_db_session()
            logger.info("数据库会话已关闭")
    except Exception as e:
        logger.error(f"关闭数据库会话时出错: {str(e)}")
    
    try:
        from app.utils.redis_helper import redis_helper
        # Redis连接不需要应用上下文
        redis_helper.close()
        logger.info("Redis连接已关闭")
    except Exception as e:
        logger.error(f"关闭Redis连接时出错: {str(e)}")
    
    logger.info("资源清理完成")

# 注册退出时的清理函数
atexit.register(cleanup_resources)

if __name__ == '__main__':
    # 设置调试模式
    debug = os.getenv('FLASK_DEBUG', 'True').lower() in ['true', '1', 'yes']
    
    # 启动应用
    logger.info(f"启动应用，调试模式: {debug}")
    app.run(host='0.0.0.0', port=5000, debug=debug) 