; Lean goal: example (f : Int → Int) (x : Int) (h1 : x > 0) (h2 : f x = x + 1) : f x > 1
; Encoding: assert hypotheses, assert negation of conclusion
(set-logic QF_UFLIA)
(declare-fun f (Int) Int)
(declare-const x Int)
(assert (> x 0))
(assert (= (f x) (+ x 1)))
(assert (not (> (f x) 1)))
(check-sat)
(get-proof)
