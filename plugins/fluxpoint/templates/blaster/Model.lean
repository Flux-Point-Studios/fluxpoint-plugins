namespace Verification

def next (n : Nat) : Nat := n + 1

def safety : Prop := ∀ n : Nat, next n > n

def feasible : Prop := ∃ n : Nat, next n > n

end Verification
