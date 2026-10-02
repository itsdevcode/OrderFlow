class ProductError(Exception):
    """Base class for product exceptions"""


class ProductAlreadyExistsError(ProductError):
    """Raised when a product with given SKU or slug already exists"""


class ProductNotFoundError(ProductError):
    """Raised when a product is not found"""


class InvalidPriceError(ProductError):
    """Raised when the product price is invalid"""
