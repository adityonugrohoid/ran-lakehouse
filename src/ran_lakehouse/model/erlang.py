"""Erlang B blocking (rule M5).

B(A, N) = (A^N / N!) / sum_{k=0..N} A^k / k!, computed with the standard
recursion B(A, 0) = 1, B(A, n) = A B(A, n-1) / (n + A B(A, n-1)), which is
exact and stable for any N.
"""

import numpy as np


def erlang_b(traffic_erl: np.ndarray, servers: np.ndarray) -> np.ndarray:
    """Blocking probability for offered traffic on a number of servers.

    Args:
        traffic_erl: Offered traffic in Erlang, any shape.
        servers: Number of servers (channels), broadcastable to traffic_erl,
            non-negative integers.

    Returns:
        Blocking probability, the broadcast shape.

    Raises:
        ValueError: If traffic is negative or servers are negative.
    """
    a = np.asarray(traffic_erl, dtype=float)
    n = np.asarray(servers)
    if (a < 0).any():
        raise ValueError("offered traffic must be non-negative")
    if (n < 0).any():
        raise ValueError("number of servers must be non-negative")
    a, n = np.broadcast_arrays(a, n)
    b = np.ones(a.shape)
    for k in range(1, int(n.max(initial=0)) + 1):
        step = a * b / (k + a * b)
        b = np.where(k <= n, step, b)
    return b
