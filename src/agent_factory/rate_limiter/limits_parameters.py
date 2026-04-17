import asyncio, os

from dotenv import load_dotenv
from aiolimiter import AsyncLimiter

from .token_bucket import LimitRegistry, NoopUmbrella, AdaptiveUmbrella

load_dotenv()


MODEL_LIMITS = {
    ### OPENAI Tier 1 Rate limit at 2025/09
    "gpt-4.1": {"TPM": 30000, "RPM": 500, "TPD": 90000},
    "gpt-4.1-mini": {"TPM": 200000, "RPM": 500, "TPD": 2000000},
    "gpt-4o": {"TPM": 30000, "RPM": 500, "TPD": 90000},
    "gpt-4o-mini": {"TPM": 200000, "RPM": 500, "TPD": 2000000},
    "text-embedding-3-large": {"TPM": 1000000, "RPM": 3000, "TPD": 3000000},
    "text-embedding-3-small": {"TPM": 1000000, "RPM": 3000, "TPD": 3000000},
    "text-ada-embedding-ada-002": {"TPM": 1000000, "RPM": 3000, "TPD": 3000000},

    ### Gemini API Free Tier Rate limit at 2026/04
    "gemma-4-31b-it": {"TPM": 30000, "RPM": 15, "RPD": 1500},
}

GLOBAL_CONCURRENCY = int(os.getenv("GLOBAL_CONCURRENCY", 6))
GLOBAL_RPM = int(os.getenv("RPM", 200))
GLOBAL_TPM = int(os.getenv("TPM", 30000))

global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
global_rpm_limiter = AsyncLimiter(GLOBAL_RPM, time_period=60)

registry = LimitRegistry(MODEL_LIMITS)

umbrella = NoopUmbrella()
# umbrella = AdaptiveUmbrella(init_tpm=sum(v["TPM"] for v in MODEL_LIMITS.values()))
