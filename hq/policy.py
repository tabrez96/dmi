"""States, who works on each one, and which moves are legal.

Two pipelines:
  products: discovery (scout <-> analyst) -> brief review -> UX (designer <-> PM)
            -> system design (architect <-> designer, architect <-> PM) -> build -> launch
  tickets:  plan -> build (engineer <-> qa, engineer <-> designer on UI) -> ship
            (engineer can ask the architect or the designer questions mid-build)

Agents never move anything themselves: they write a result file and HQ
applies the transition after checking it here.
"""

# -- products --------------------------------------------------------------------

DISCOVERING = "discovering"          # scout and analyst take turns
BRIEF_REVIEW = "brief_review"        # PM gate: build this?
UX_DESIGNING = "ux_designing"        # designer's turn: flows, screens, mockups
UX_REVIEW = "ux_review"              # PM's turn: discuss or approve the UX
DESIGNING = "designing"              # architect's turn (or designer's, answering an architect concern)
DESIGN_REVIEW = "design_review"      # PM's turn: discuss or approve the design
TICKET_REVIEW = "ticket_review"      # PM's turn: go through the drafted tickets, comment or approve
BUILDING = "building"                # tickets in flight
READY_TO_LAUNCH = "ready_to_launch"  # PM gate: every ticket shipped
LAUNCHED = "launched"
P_BLOCKED = "blocked"
P_REJECTED = "rejected"

PRODUCT_STATES = [DISCOVERING, BRIEF_REVIEW, UX_DESIGNING, UX_REVIEW, DESIGNING, DESIGN_REVIEW, TICKET_REVIEW,
                  BUILDING,
                  READY_TO_LAUNCH, P_BLOCKED, LAUNCHED, P_REJECTED]

PRODUCT_TRANSITIONS = {
    DISCOVERING: {BRIEF_REVIEW, P_BLOCKED, P_REJECTED},
    BRIEF_REVIEW: {UX_DESIGNING, DISCOVERING, P_REJECTED},
    UX_DESIGNING: {UX_REVIEW, P_BLOCKED},
    UX_REVIEW: {UX_DESIGNING, DESIGNING, P_REJECTED},
    DESIGNING: {DESIGN_REVIEW, TICKET_REVIEW, P_BLOCKED},
    DESIGN_REVIEW: {DESIGNING, TICKET_REVIEW, P_REJECTED},
    TICKET_REVIEW: {DESIGNING, BUILDING, P_REJECTED},
    BUILDING: {READY_TO_LAUNCH},
    READY_TO_LAUNCH: {LAUNCHED, BUILDING},
    P_BLOCKED: {DISCOVERING, UX_DESIGNING, DESIGNING, P_REJECTED},
    LAUNCHED: set(),
    P_REJECTED: set(),
}

BUILT_STATES = (BUILDING, READY_TO_LAUNCH, LAUNCHED)    # the design is done: a change to the product is a ticket
PRODUCT_NEEDS_PM = [BRIEF_REVIEW, UX_REVIEW, DESIGN_REVIEW, TICKET_REVIEW, READY_TO_LAUNCH, P_BLOCKED]

# -- tickets ---------------------------------------------------------------------

PROPOSED = "proposed"            # waiting for the PM
APPROVED = "approved"            # planner writes the detailed plan
PLAN_REVIEW = "plan_review"      # PM gate (only when gates.plan is on)
READY_FOR_DEV = "ready_for_dev"  # engineer's turn
CLARIFYING = "clarifying"        # engineer asked the architect or designer a question
IN_QA = "in_qa"                  # qa's turn
DESIGN_QA = "design_qa"          # designer reviews a UI ticket against the UX spec
ESCALATED = "escalated"          # the manager's turn: a stuck conversation, or the PM asked
READY_TO_SHIP = "ready_to_ship"  # PM gate before merge (only when gates.ship is on)
PR_REVIEW = "pr_review"          # the planner triages CodeRabbit's comments on the open PR
SHIPPED = "shipped"
BLOCKED = "blocked"              # conversation hit its round limit, a question, a budget
REJECTED = "rejected"

STATES = [PROPOSED, APPROVED, PLAN_REVIEW, READY_FOR_DEV, CLARIFYING, IN_QA, DESIGN_QA, ESCALATED,
          READY_TO_SHIP, PR_REVIEW, BLOCKED, SHIPPED, REJECTED]

# (role, state): the role works on tickets in that state
PICKUP = [
    ("planner", APPROVED),
    ("planner", CLARIFYING),     # only when the question was for the architect
    ("designer", CLARIFYING),    # only when the question was for the designer
    ("planner", PR_REVIEW),
    ("engineer", READY_FOR_DEV),     # unless the ticket's builder is the designer
    ("designer", READY_FOR_DEV),     # only a ticket whose builder is the designer: design work
    ("qa", IN_QA),
    ("designer", DESIGN_QA),
    ("manager", ESCALATED),
]

NEEDS_PM = [PROPOSED, PLAN_REVIEW, READY_TO_SHIP, BLOCKED]

TRANSITIONS = {
    PROPOSED: {APPROVED, REJECTED},
    APPROVED: {PLAN_REVIEW, READY_FOR_DEV, BLOCKED, REJECTED},
    PLAN_REVIEW: {READY_FOR_DEV, APPROVED, REJECTED},
    READY_FOR_DEV: {IN_QA, CLARIFYING, ESCALATED, BLOCKED, READY_TO_SHIP},   # READY_TO_SHIP: design work has no QA
    CLARIFYING: {READY_FOR_DEV, BLOCKED},
    IN_QA: {DESIGN_QA, READY_TO_SHIP, SHIPPED, READY_FOR_DEV, ESCALATED, BLOCKED},
    DESIGN_QA: {READY_TO_SHIP, SHIPPED, READY_FOR_DEV, ESCALATED, BLOCKED},
    ESCALATED: {IN_QA, READY_FOR_DEV, APPROVED, BLOCKED},
    READY_TO_SHIP: {SHIPPED, READY_FOR_DEV, PR_REVIEW, BLOCKED},
    PR_REVIEW: {READY_TO_SHIP, READY_FOR_DEV, SHIPPED, BLOCKED},
    BLOCKED: {APPROVED, READY_FOR_DEV, CLARIFYING, IN_QA, DESIGN_QA, READY_TO_SHIP, ESCALATED, REJECTED},
    SHIPPED: set(),
    REJECTED: set(),
}


class IllegalTransition(Exception):
    pass


def check(from_state, to_state, product=False):
    table = PRODUCT_TRANSITIONS if product else TRANSITIONS
    if to_state not in table.get(from_state, set()):
        raise IllegalTransition(f"{from_state} -> {to_state} is not allowed")


def after_plan(cfg):
    return PLAN_REVIEW if cfg["gates"]["plan"] else READY_FOR_DEV


def max_rounds(cfg):
    return cfg["conversation"]["max_rounds"]


def after_qa_fail(ticket, cfg, repeated=False):
    """Engineer <-> QA/designer share max_rounds failed rounds; a repeated failure escalates early."""
    if repeated or ticket["qa_failures"] + 1 >= max_rounds(cfg):
        return BLOCKED
    return READY_FOR_DEV


def pm_approve_target(state):
    return {
        PROPOSED: APPROVED,
        PLAN_REVIEW: READY_FOR_DEV,
        READY_TO_SHIP: SHIPPED,
        PR_REVIEW: SHIPPED,
    }.get(state)


def pm_reject_target(state):
    # Rejecting a plan sends it back for re-planning; rejecting a build sends
    # it back to the engineer with your note as feedback.
    return {
        PROPOSED: REJECTED,
        APPROVED: REJECTED,        # cancel a ticket before it has a plan
        PLAN_REVIEW: APPROVED,
        READY_TO_SHIP: READY_FOR_DEV,
        PR_REVIEW: READY_FOR_DEV,
        BLOCKED: REJECTED,
    }.get(state)


def product_approve_target(state):
    return {
        BRIEF_REVIEW: UX_DESIGNING,
        UX_REVIEW: DESIGNING,
        DESIGN_REVIEW: TICKET_REVIEW,
        TICKET_REVIEW: BUILDING,
        READY_TO_LAUNCH: LAUNCHED,
    }.get(state)
