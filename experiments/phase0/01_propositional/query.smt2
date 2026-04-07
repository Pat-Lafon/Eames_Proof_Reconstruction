; Lean goal: example (p q : Prop) (hp : p) (hpq : p → q) : q
; Encoding: assert p, assert (p → q), assert (not q), check unsat
(set-logic QF_UF)
(declare-const p Bool)
(declare-const q Bool)
(assert p)
(assert (=> p q))
(assert (not q))
(check-sat)
(get-proof)
