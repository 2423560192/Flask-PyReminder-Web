# 操作类型
OP_TYPES = {
    'CREATE': 'create',
    'UPDATE': 'update',
    'DELETE': 'delete'
}

# Redis键名前缀
KEY_PREFIX = {
    'USERS': 'sync:users:',
    'TOKENS': 'sync:tokens:',
    'TASKS': 'sync:tasks:'
}

# 同步锁，防止多个进程同时同步
SYNC_LOCK_KEY = 'sync:lock'
# 同步状态键，记录上次同步时间
SYNC_STATUS_KEY = 'sync:status'

# 任务相关的Redis键名
TASKS_KEY = "tasks_hash"  # 存储任务的Redis哈希表键名
PENDING_TASKS_KEY = "pending_tasks"  # 存储待处理任务的Redis有序集合键名 