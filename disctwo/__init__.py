"""Disc Two — file a DVD's special features into your media library.

Point it at an ISO. It identifies the disc, works out which titles are extras,
finds their real names, and writes them where Plex looks for them.

It does not rip. Whatever produced the ISO — ISOHungry, ARM, MakeMKV, dd — is
none of its business, and that is the point: the naming and filing problem is
the same whichever ripper you use, and nobody had solved it.
"""

__version__ = "0.1.0"
