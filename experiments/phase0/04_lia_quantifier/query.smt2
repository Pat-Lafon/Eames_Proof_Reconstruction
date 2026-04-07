; Lean goal: example : ∀ x : Int, 0 ≤ x → 0 ≤ x + 1
; Encoding: assert negation — exists x such that 0 ≤ x but not (0 ≤ x + 1)
(set-logic LIA)
(assert (exists ((x Int)) (and (>= x 0) (not (>= (+ x 1) 0)))))
(check-sat)
(get-proof)
