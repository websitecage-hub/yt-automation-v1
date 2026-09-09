Meme cache. adapters.meme_img() writes every generated meme here keyed by a
hash of its prompt, and reads it back on the next run instead of paying for
the same picture twice -- produce.yml commits this directory for exactly that
reason. Dropping your own file in as {key}.png/.jpg overrides the generated
one permanently: a hand-picked meme always beats a bought one.
