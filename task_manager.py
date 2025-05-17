# task_manager.py

class TaskManager:
    def __init__(self):
        self.cancelled_users = set()

    def cancel(self, user_id: int):
        self.cancelled_users.add(user_id)

    def is_cancelled(self, user_id: int) -> bool:
        return user_id in self.cancelled_users

    def clear(self, user_id: int):
        self.cancelled_users.discard(user_id)

task_manager = TaskManager()
