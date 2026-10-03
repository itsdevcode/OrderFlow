class WarehouseError(Exception):
    pass

class WarehouseAlreadyExistsError(WarehouseError):
    pass

class WarehouseNotFoundError(WarehouseError):
    pass
