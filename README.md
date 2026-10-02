# A refund racing an autopay charge

[![CI](https://github.com/mghabin/refund-autopay-race/actions/workflows/ci.yml/badge.svg)](https://github.com/mghabin/refund-autopay-race/actions/workflows/ci.yml)

A store confirms a partial refund while autopay is charging the same payment plan. That overlap is expected: Affirm's [US help page](https://helpcenter.affirm.com/s/article/returns-and-cancellations) says "Until your refund is applied, any upcoming payments are still due." and the [UK one](https://help.uk.affirm.com/article/get-a-refund-sp-uk) says "While you’re waiting for your refund, continue to make any payments that are due." The US page gives the rule: "We’ll apply the refund to the final remaining payments first. Any remaining refund amount will go back to your payment method." The UK page describes the same effect: a smaller refund "might not lower your next payment", and the customer can expect "fewer payments, a smaller final payment or both". This repo models that race in Python and shows that nobody is charged twice and no money is lost.

## The worked example

A plan of 4 payments of 100.00. Payment 1 is paid, and autopay is charging payment 2 for 100.00 when a 250.00 refund arrives. Taking the last payments first, payments 4 and 3 drop to 0 and payment 2 drops to 50.00. What happens next depends on the charge:

| Charge outcome | Payment 2 | Back to the customer |
| --- | --- | --- |
| Succeeds | 100.00 collected for a payment now worth 50.00 | 50.00 |
| Fails | Still due at 50.00, so the next attempt charges 50.00 | Nothing |
| Times out | Unchanged until a status lookup with the same idempotency key says which of the two rows above happened. No second attempt can start meanwhile | As in that row |

A 30.00 refund only lowers payment 4 to 70.00 and leaves the charge alone. A 350.00 refund clears payments 4, 3 and 2 and takes 50.00 off payment 1, which is already paid, so 50.00 goes back right away. When the charge for payment 2 succeeds, its 100.00 goes back too and the plan closes. A refund that would take total refunds past the plan total is rejected.

## Guarantees

The rules are frozen dataclasses and pure functions in `plan.py`. `store.py` keeps plans in SQLite, `service.py` runs the handlers (load, apply a rule, save if the version still matches, retry), `processor.py` has a fake processor that honours idempotency keys, and `checks.py` has the checks a scheduled job would run.

| Guarantee | Enforced in | Proved by |
| --- | --- | --- |
| 1. Money is integer minor units, and a plan's payments add up exactly to its total. A leftover cent goes on the first payment, so the later ones are all equal. | `split_total`, `apply_refund` | `test_leftover_cent_goes_on_the_first_payment`, the property test |
| 2. The same refund delivered twice does nothing the second time. | `refunds` table keyed on plan and the store's refund ID, written in the same transaction as the plan change; `refund_steps` checks it after loading the plan | `test_same_refund_twice_does_nothing_the_second_time`, `test_every_order_of_a_charge_and_a_refund_delivered_twice_is_safe` |
| 3. A refund and autopay on the same plan, in any order or overlap, never charge twice or lose money. | `version` column and `UPDATE ... WHERE id = ? AND version = ?` in `Store.save`; handlers reload and retry up to 5 times | `test_every_order_of_two_workers_and_a_refund_is_safe`, and the two naive store tests |
| 4. Every charge attempt has an idempotency key made of plan, payment and attempt number. A timeout is settled by a status lookup with the same key, never by a new attempt. | `charge_key`, `start_charge` (one attempt in flight per plan), `call_processor` | `test_timeout_is_settled_by_a_lookup_with_the_same_key`, the property test |
| 5. The books balance after every step: collected, minus sent back, plus still due, equals the plan total minus refunds. With nothing in flight, the processor's captures match what the plan collected. | `check_books` and `check_processor`, which read refunds and payouts from their own tables. Payouts are written in the same transaction as the plan change (an outbox). | Run after every action of the property test and after every step of every interleaving |
| 6. Market differences (currency, where money goes back, the published window) sit behind one interface. A new market is one new class. | `Market` in `markets.py`. The windows are the most the pages allow once the store's refund comes in: 3 business days in the US, 10 in the UK | `test_business_days_skip_weekends`, `test_refund_applied_after_its_window_is_flagged`, `test_uk_money_goes_back_in_pounds` |

The property test runs random plans (2 to 12 payments, random totals and markets) through random refunds and charge outcomes, and also checks that refunds come off strictly last first and that nothing goes back while a payment is still owed.

The interleaving test is the one I care about most. Each handler is a generator that pauses wherever another handler could get in: after it loads the plan, and while its charge is with the processor. The test runs every possible order of those steps for two autopay workers on payment 2 and the 250.00 refund, on a fresh in-memory store each time: between 2,904 and 3,894 orders for each of the four processor outcomes. There are no threads, so a failure prints an order that reproduces exactly. A naive store that saves without the version check fails the same checks. In one order, the refund's change to payment 2 is overwritten by the charge's stale save, and the 50.00 never goes back.

## Trade-offs

There are three common ways to stop the refund and the charge from overwriting each other.

- **Row lock** (`SELECT ... FOR UPDATE`). Writers queue on the plan row, which is easy to reason about. The lock must not be held across the processor call, which can take seconds, so a charge still needs two short transactions and an in-flight marker. I'd pick it when one plan sees a lot of contention and the database handles locks well.
- **Version check**, which is what I used. Nothing is held, and a conflict costs one reload and a retry. Conflicts on a single plan are rare here (a refund landing in the few seconds a charge is out), it works on any store with a conditional update, and SQLite has no `FOR UPDATE` anyway.
- **One queue per plan**, for example a log partitioned by plan ID. Changes to a plan run one at a time, so there are no conflicts at all. I'd pick it in a system that is already event driven. The cost is running the queue, and a slow processor call holds up everything behind it for that plan.

## What I'd monitor in production

- The balance check as a scheduled job over every plan, alerting on any problem it finds.
- Any payment captured more than once, from the processor's settlement report.
- Charges in flight for longer than the processor's timeout, since their outcome is unknown.
- Refund lag in business days against each market's window (`late_refunds`).
- The version conflict rate and how often handlers give up after 5 tries. A jump points at a hot plan or a retry storm.

## Not done yet

- Interest. Plans here are interest-free. The US page says "minus any interest you’ve paid" and the UK page says "minus any payment towards the total cost of credit you have made", and I haven't modelled either.
- Bank holidays. Business days skip weekends only.
- Partial captures. A charge collects the whole attempt or nothing.
- Real queues and storage. SQLite runs in memory, handlers are called directly, and nothing sends the payouts yet.
- A status lookup that can't answer yet, and a sweeper for attempts left in flight by a crash. Before settling one as failed it must be sure the first request can't still land.
- More markets.

## Open questions

These are what I'd ask a team that runs this for real:

1. When a refund covers a payment that's mid-charge, do you cancel the debit or let it settle and send the difference back? I let it settle.
2. Is a refund applied when the store confirms it, or when the money arrives from the store?
3. When money can't go back to the original payment method, how is the fallback tracked against the published window?

## Running it

With Python 3.12 or later:

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Independent demo based on Affirm's public help pages. Not affiliated with Affirm.
