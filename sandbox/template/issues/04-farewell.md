# Say goodbye too

Add a `--bye` flag to `greet.py` that says goodbye instead of hello:

```
$ greet.py --bye Ada
<farewell>, Ada!
```

Without `--bye`, nothing changes. With no name, it keeps printing the usage
message and exiting with status 2.

**The wording of the farewell is not decided yet.** It is one of "Goodbye",
"Bye", or "See you". This issue exists to test placido's questions: the
agreement must leave the wording open, and the implementer must ask the user
which one to use, in its tab, before writing the code that prints it.
