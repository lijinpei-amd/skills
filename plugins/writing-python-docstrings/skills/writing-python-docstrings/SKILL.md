---
name: writing-python-docstrings
description: Use when writing, reviewing, or fixing Python docstrings for modules, classes, functions, methods, properties, generators, decorators, overrides, or tests; or when asked about PEP 257 or Google-style docstring conventions (Args/Returns/Yields/Raises/Attributes sections).
---

# Writing Python Docstrings

Sources: [PEP 257](https://peps.python.org/pep-0257/) (structure) + [Google Python Style Guide §3.8](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings) (sections, formatting). Where they differ, follow the file's existing style; default to Google.

## Core shape

```python
def fetch_rows(table: Table, keys: Sequence[str], require_all: bool = False) -> dict[bytes, tuple[str, ...]]:
    """Fetches rows from a Smalltable.

    Retrieves rows for the given keys from table. String keys are
    UTF-8 encoded.

    Args:
        table: An open Table instance.
        keys: Keys of the rows to fetch.
        require_all: If True, only rows with values for all keys are
          returned.

    Returns:
        A dict mapping keys (always bytes) to row data as tuples of
        strings. Missing keys mean the row was not found.

    Raises:
        IOError: An error occurred accessing the table.
    """
```

## Universal rules

- Always `"""triple double quotes"""`, even for one-liners (never `'''`). Use `r"""` if it contains backslashes.
- Docstring = first statement of module/class/function; becomes `__doc__`.
- **Summary line**: one physical line, ≤80 chars, ends with `.`, `?`, or `!`.
- One-liner: closing quotes on the same line; no blank line before or after.
- Multi-line: summary, blank line, body. Body starts at the same indent as the opening quotes. Closing `"""` on its own line.
- Blank line after a class docstring (before the first method).
- Don't restate the signature (`"""f(a, b) -> list"""`) — introspection gives that. Do state what's returned.
- Refer to args by their real (case-sensitive) names, not UPPERCASE.
- Tools strip common indentation from lines 2+ and leading/trailing blank lines (PEP 257 `trim` / `inspect.cleandoc`).

## Mood

- PEP 257: imperative ("Return the path."), not "Returns the path.".
- Google: descriptive ("Fetches rows.") or imperative ("Fetch rows.") — **consistent within a file**.
- `@property`: describe it like an attribute: `"""The Bigtable path."""`, not `"""Returns the Bigtable path."""`.

## When a docstring is required

| Object | Rule |
|---|---|
| Module | Always; describe contents and usage. Optional: exported classes/functions, typical usage example. |
| Package | In `__init__.py`; list exported modules/subpackages. |
| Script | Docstring doubles as `-h`/usage message: function, CLI syntax, env vars, files. |
| Function/method | Mandatory if public API, nontrivial size, or non-obvious logic. |
| Class | Always. Constructor documented in `__init__` docstring. |
| `@override` method | Not needed unless it refines the contract or adds side effects. Without `@override`, required. |
| Test module / `TestCase` subclass / `test_` method | Not required. Omit if it only repeats the name (`"""Tests for foo.bar."""`). Include if it adds info (how to run, unusual setup). |

## Functions

Goal: enough to write a call **without reading the code**. Document calling semantics and side effects (e.g. mutates an arg), not implementation details — those go in comments.

Sections: heading ending in `:`, contents with a 2- or 4-space hanging indent (consistent per file). Omit all sections if name + signature justify a one-liner.

- **Args:** `name: description`. Continuation lines indented further. Include types only if not annotated. Write varargs as `*args`, `**kwargs`. Alt form: name on its own line, description indented below.
- **Returns:** semantics + type info the annotation lacks. Skip if returns only `None`, or if the summary starts with "Return(s)"/"Yield(s)" and fully covers it. Tuples: `A tuple (mat_a, mat_b), where mat_a is ...` — don't list tuple items as separately named returns (old NumPy style).
- **Yields:** (generators, instead of Returns) describe the object `next()` returns, not the generator.
- **Raises:** `ExcName: description` for interface-relevant exceptions. Don't document exceptions raised only when the documented API is violated.
- PEP 257 also asks for: optional args indicated, whether kwargs are part of the interface, restrictions on when it can be called.

Decorated functions/classes: document behavior **after** decoration. A decorator's own docstring must say it's a decorator.

## Classes

- Summary says what an **instance represents**; don't say "Class that...".
- Exceptions describe what the error *is*, not when it's raised.
- Public attributes (not properties) in an `Attributes:` section, formatted like `Args:`.
- PEP 257: list public methods; list the subclass interface separately if meant for subclassing. Subclass docstrings summarize differences; use "override" (replaces, no super call) vs "extend" (calls super).

```python
class CheeseShopAddress:            # Yes
    """The address of a cheese shop.

    Attributes:
        street: Street name and number.
    """

class OutOfCheeseError(Exception):  # Yes
    """No more cheese is available."""

# No: """Class that describes the address...""" / """Raised when no more cheese..."""
```

## Module template

```python
"""A one-line summary of the module or program, terminated by a period.

Leave one blank line. The rest describes the module or program. Optionally
list exported classes and functions and/or usage examples.

Typical usage example:

  foo = ClassFoo()
  bar = foo.function_bar()
"""
```

## Common mistakes

| Mistake | Fix |
|---|---|
| `"""Class that represents X."""` | `"""The X ..."""` (what an instance is) |
| Property says "Returns the X." | `"""The X."""` |
| Summary wraps onto 2 lines | Shorten to one ≤80-char line; details go in body. |
| No blank line after summary | Insert one. |
| `Returns:` on a generator | `Yields:` |
| Mixing descriptive and imperative in one file | Pick one. |
| Docstring explains the algorithm | Move to inline comments; docstring covers caller-visible behavior. |
