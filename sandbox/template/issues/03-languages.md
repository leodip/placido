# Greet in other languages

`greet.py Ada` always greets in English. Add a `--lang` option so that people can
be greeted in Spanish and Portuguese too:

```
$ greet.py Ada
Hello, Ada!
$ greet.py --lang es Ada
¡Hola, Ada!
$ greet.py --lang pt Ada
Olá, Ada!
```

English stays the default. An unknown language is an error: print the usage
message and exit with status 2.

People who always want the same language should not have to type it every time:
the `GREET_LANG` environment variable sets the default language, and `--lang`
still wins over it.

Document the languages and `GREET_LANG` in the README.
