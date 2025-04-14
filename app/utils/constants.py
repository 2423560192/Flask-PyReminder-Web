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