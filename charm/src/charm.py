#!/usr/bin/env python3
"""Run the stock Flask PaaS charm."""

import ops
from paas_charm.flask import Charm

if __name__ == '__main__':
    ops.main(Charm)
