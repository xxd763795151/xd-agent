import pytest

from xd_agent.features.greeting.models import GreetInput
from xd_agent.features.greeting.service import GreetingService, InvalidNameError


def test_greeting_is_independent_of_adapters() -> None:
    result = GreetingService("Hello").execute(GreetInput(name=" Ada "))
    assert result.message == "Hello, Ada!"


def test_blank_name_is_application_error() -> None:
    with pytest.raises(InvalidNameError):
        GreetingService("Hello").execute(GreetInput(name="  "))
