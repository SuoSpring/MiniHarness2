class HarnessError(Exception):
    """可直接向调用者解释的业务错误。"""


class Conflict(HarnessError):
    pass


class NotFound(HarnessError):
    pass


class BudgetExceeded(HarnessError):
    pass
