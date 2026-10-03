class InventoryError(Exception):
    pass

class InventoryNotFoundError(InventoryError):
    pass

class InsufficientStockError(InventoryError):
    pass
