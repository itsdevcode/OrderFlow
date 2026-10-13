from decimal import Decimal
import hashlib
from typing import override

from app.services.payment_provider import PaymentProvider, ProviderPaymentResult


class FakePaymentProvider(PaymentProvider):
    """
    Deterministic fake payment provider for local development and tests.
    Does not use random generation:
    - Generates deterministic provider_payment_id based on idempotency_key.
    - If idempotency_key contains 'fail' (case-insensitive), returns failure result.
    - If idempotency_key contains 'error' (case-insensitive), raises an exception.
    """

    _provider_name: str

    def __init__(self, provider_name: str = "fake_provider") -> None:
        self._provider_name = provider_name

    @property
    @override
    def provider_name(self) -> str:
        return self._provider_name

    @override
    async def create_charge(
        self,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        order_id: int,
    ) -> ProviderPaymentResult:
        if "error" in idempotency_key.lower():
            raise RuntimeError(f"Simulated network/provider outage for key: {idempotency_key}")

        token_hash = hashlib.sha256(f"{order_id}:{idempotency_key}".encode()).hexdigest()[:16]
        provider_payment_id = f"pay_fake_{token_hash}"

        if "fail" in idempotency_key.lower():
            return ProviderPaymentResult(
                provider_payment_id=provider_payment_id,
                success=False,
                error_message="Payment rejected by fake provider",
            )

        return ProviderPaymentResult(
            provider_payment_id=provider_payment_id,
            success=True,
            error_message=None,
        )

    @override
    async def refund_charge(
        self,
        provider_payment_id: str,
        amount: Decimal,
    ) -> ProviderPaymentResult:
        if "fail" in provider_payment_id.lower():
            return ProviderPaymentResult(
                provider_payment_id=f"ref_{provider_payment_id}",
                success=False,
                error_message="Refund rejected by fake provider",
            )

        return ProviderPaymentResult(
            provider_payment_id=f"ref_{provider_payment_id}",
            success=True,
            error_message=None,
        )
