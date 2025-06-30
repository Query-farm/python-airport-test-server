import collections.abc
from collections.abc import Iterable, Iterator
from typing import Any, Generic, TypeVar, overload

# Type variable for the dictionary's value type
VT = TypeVar("VT")

# Type variable for the value in the fromkeys classmethod
_VT_FK_OUT = TypeVar("_VT_FK_OUT")

# Represents the type of None, for precise typing in fromkeys
NoneType = type(None)


class CaseInsensitiveDict(collections.abc.MutableMapping[str, VT], Generic[VT]):
    """
    A dictionary that stores string keys in a case-insensitive manner.

    The original case of the key is preserved upon first insertion or if a key
    is updated with a new casing. All lookups (get, set, delete, in) are
    case-insensitive with respect to string keys.

    Example:
        cid = CaseInsensitiveDict({'Content-Type': 'application/json'})
        print(cid['content-type'])  # Output: application/json
        print('CONTENT-TYPE' in cid) # Output: True

        cid['content-type'] = 'text/plain' # Updates value and preserved key case
        print(list(cid.keys())) # Output: ['content-type']
        print(cid) # Output: CaseInsensitiveDict({'content-type': 'text/plain'})
    """

    # Overloads for __init__ to guide type checkers, similar to `dict`
    # @overload
    # def __init__(self, **kwargs: VT) -> None: ...
    # @overload
    # def __init__(self, map: Mapping[str, VT], **kwargs: VT) -> None: ...
    # @overload
    # def __init__(self, iterable: Iterable[Tuple[str, VT]], **kwargs: VT) -> None: ...

    def __init__(self, *args: Any, **kwargs: VT) -> None:
        """
        Initializes the CaseInsensitiveDict.
        It can be initialized like a standard dict:
        - CaseInsensitiveDict(mapping, **kwargs)
        - CaseInsensitiveDict(iterable, **kwargs)
        - CaseInsensitiveDict(**kwargs)
        """
        self._store: dict[str, VT] = {}
        # _key_map maps lowercase key to the actual cased key used in _store
        self._key_map: dict[str, str] = {}

        # Populate from args and kwargs using the update method,
        # which in turn uses our __setitem__.
        # dict(*args, **kwargs) creates a standard dictionary from the input,
        # which self.update will then process.
        self.update(dict(*args, **kwargs))

    def __setitem__(self, key: str, value: VT) -> None:
        """
        Set d[key] to value. Key lookups are case-insensitive.
        The case of the key used in this operation is preserved.
        """
        if not isinstance(key, str):
            raise TypeError(f"Keys must be strings for CaseInsensitiveDict, got {type(key).__name__}")

        lower_key = key.lower()

        # If a key with the same lowercase form but different actual casing
        # already exists, we need to remove the old entry from _store
        # because _store's keys are case-sensitive.
        # This ensures the new key's casing is preserved.
        if lower_key in self._key_map:
            original_cased_key = self._key_map[lower_key]
            if original_cased_key != key:
                # The new key's casing is different; remove the old one from _store.
                # The value will be associated with the new key's casing.
                del self._store[original_cased_key]

        # Store the value with the new key (preserving its case)
        self._store[key] = value
        # Update the map to point to the current key's casing
        self._key_map[lower_key] = key

    def __getitem__(self, key: str) -> VT:
        """Return the value for key (case-insensitive lookup). Raises KeyError if not found."""
        if not isinstance(key, str):
            raise TypeError(f"Keys must be strings for CaseInsensitiveDict, got {type(key).__name__}")

        lower_key = key.lower()
        if lower_key not in self._key_map:
            raise KeyError(f"Key not found: '{key}'")

        # Retrieve using the original (preserved) cased key
        original_cased_key = self._key_map[lower_key]
        return self._store[original_cased_key]

    def __delitem__(self, key: str) -> None:
        """Delete d[key]. Key lookups are case-insensitive. Raises KeyError if not found."""
        if not isinstance(key, str):
            raise TypeError(f"Keys must be strings for CaseInsensitiveDict, got {type(key).__name__}")

        lower_key = key.lower()
        if lower_key not in self._key_map:
            raise KeyError(f"Key not found: '{key}'")

        original_cased_key = self._key_map[lower_key]
        del self._store[original_cased_key]
        del self._key_map[lower_key]

    def __iter__(self) -> Iterator[str]:
        """Return an iterator over the keys (with preserved casing)."""
        return iter(self._store.keys())

    def __len__(self) -> int:
        """Return the number of items in the dictionary."""
        return len(self._store)

    def __repr__(self) -> str:
        """Return a string representation of the dictionary."""
        return f"{type(self).__name__}({self._store})"

    def __eq__(self, other: object) -> bool:
        """
        Compare this dictionary with another mapping for equality.
        Comparison is case-insensitive for keys.
        Both keys and values must match.
        """
        if not isinstance(other, collections.abc.Mapping):
            return NotImplemented

        if len(self) != len(other):
            return False

        # Convert self to a canonical form (all string keys lowercased)
        self_canonical: dict[str, VT] = {k.lower(): v for k, v in self.items()}

        other_canonical: dict[str, Any] = {}
        try:
            for o_key, o_value in other.items():
                if isinstance(o_key, str):
                    other_canonical[o_key.lower()] = o_value
                else:
                    # If `other` contains any non-string key, it cannot be
                    # equal to this CaseInsensitiveDict, which only supports string keys.
                    return False
        except AttributeError:
            # This might happen if o_key.lower() fails unexpectedly,
            # or if other.items() is problematic.
            return NotImplemented

        return self_canonical == other_canonical

    def copy(self) -> "CaseInsensitiveDict[VT]":
        """Return a shallow copy of the dictionary."""
        # The __init__ method can conveniently handle another CaseInsensitiveDict
        # or any mapping to create a new instance.
        return type(self)(self)

    # Overloads for fromkeys to ensure correct return type inference
    @overload
    @classmethod
    def fromkeys(cls, iterable: Iterable[str]) -> "CaseInsensitiveDict[NoneType]": ...

    @overload
    @classmethod
    def fromkeys(cls, iterable: Iterable[str], value: _VT_FK_OUT) -> "CaseInsensitiveDict[_VT_FK_OUT]": ...

    @classmethod
    def fromkeys(cls, iterable: Iterable[str], value: Any = None) -> "CaseInsensitiveDict[Any]":
        """
        Create a new dictionary with keys from iterable and values set to value.
        If value is not specified, it defaults to None.
        Keys in the iterable must be strings.
        """
        instance = cls()  # Creates an empty CaseInsensitiveDict
        for key in iterable:
            if not isinstance(key, str):
                raise TypeError(f"All keys in iterable for fromkeys must be strings, got {type(key).__name__}")
            # This uses the instance's __setitem__, ensuring case-insensitivity logic
            instance[key] = value
        return instance
