use aiken/fuzz

fn twice(n: Int) -> Int {
  n + n
}

test unit_twice() {
  twice(21) == 42
}

test property_twice(n: Int via fuzz.int()) {
  twice(n) == n * 2
}
