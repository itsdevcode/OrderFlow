from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ProviderPaymentResult:
    provider_payment_id: str
    success: bool
    error_message: str | None = None


class PaymentProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the payment provider."""
        pass

    @abstractmethod
    async def create_charge(
        self,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        order_id: int,
    ) -> ProviderPaymentResult:
        """Create or initiate a charge attempt with the provider."""
        pass

    @abstractmethod
    async def refund_charge(
        self,
        provider_payment_id: str,
        amount: Decimal,
    ) -> ProviderPaymentResult:
        """Refund an existing charge."""
        pass
