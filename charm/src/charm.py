#!/usr/bin/env python3
"""Run the Flask PaaS charm with the shared password mapping."""

import ops
from paas_charm.flask.charm import Charm, FlaskConfig
from pydantic import Field, field_validator


class PokerConfig(FlaskConfig):
    # PaaS reserves app-* from implicit environment exports.
    app_password_value: str | None = Field(default=None, alias='app-password', repr=False)

    @field_validator('app_password_value', mode='before')
    @classmethod
    def unpack_password(cls, content):
        return content.get('value') if isinstance(content, dict) else content


class PokerCharm(Charm):
    framework_config_class = PokerConfig

if __name__ == '__main__':
    ops.main(PokerCharm)
