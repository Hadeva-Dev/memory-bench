"""proj-move: from Apartment 3B, 2210 Sorrel Street to 818 Kestrel Avenue."""

P = "principal"
J = "proj-move"
FIN = ["finance"]
PMF = ["private_message", "finance"]


def build(w):
    e = w.ev("03-03 19:00", "typed",
             "Evening at the computer. Joana starts the house hunt: the Sorrel Street lease ends May 31, current rent "
             "is 2,150 a month, they want 3 bedrooms, a yard for Waffles, inside the Fernhill Elementary boundary, "
             "and no more than 2,900 a month. In passing she gives the building's front door code for the new "
             "cleaner.", J)
    e.f(P, "lease_end_sorrel", "Sorrel Street lease ends May 31", h="lease_end")
    e.f(P, "rent", "2,150 dollars a month at Sorrel Street", sens=FIN, h="rent0")
    e.f(P, "house_hunt_criteria", "3 bedrooms, a yard for Waffles, inside the Fernhill Elementary boundary",
        h="criteria")
    e.f(P, "max_rent_budget", "2,900 dollars a month", sens=FIN, h="maxrent")
    e.f(P, "sorrel_building_door_code", "4839", sens=["secret"], h="doorcode")

    e = w.ev("03-05 10:15", "inbound_text",
             "Text from landlord Gerald Pike: if they renew, rent goes to 2,450 from June 1.", J)
    e.f("p022", "relationship_to_principal", "Landlord at Sorrel Street", by="p022", h="gerald_rel")
    e.f("p022", "renewal_offer", "Rent would rise to 2,450 a month from June 1 on renewal", by="p022", sens=PMF,
        h="renewal")

    e = w.ev("03-07 21:00", "voice",
             "Browsing listings on the couch with Owen. Owen (bystander): 'I want a gas stove, that's my one thing.' "
             "Joana: the yard matters more. Joke from Joana: 'we could just move into the library, it has a yard and "
             "a thousand books'.", J)
    e.f("p001", "house_wish", "Owen wants a gas stove in the new place", by="p001", h="owen_gas")

    e = w.ev("03-14 13:00", "web",
             "Assistant reads a rental listing site while Joana searches: 818 Kestrel Avenue, 3 bed, 1.5 bath, fenced "
             "yard, gas range, 2,875 a month, available June 1, owner Priya Sandoval.", J)
    e.f("house.kestrel", "listing", "818 Kestrel Avenue: 3 bed, 1.5 bath, fenced yard, gas range, available June 1",
        by="web:rentals-nw.example", h="kestrel_listing")
    e.f("house.kestrel", "listed_rent", "2,875 dollars a month", by="web:rentals-nw.example", h="kestrel_rent_listed")
    e.f("p023", "relationship_to_principal", "Owner of 818 Kestrel Avenue (prospective landlord)",
        by="web:rentals-nw.example", h="priya_rel0")

    e = w.ev("03-18 20:00", "typed",
             "Booking viewings: 4402 Thistle Court on Sunday March 22 at 11:00 (2,700, 3 bed, no yard) and 818 "
             "Kestrel Avenue on Monday March 23 at 10:00.", J)
    e.f(P, "viewing_thistle", "4402 Thistle Court, Sunday March 22 at 11:00 (2,700 a month, 3 bed, no yard)",
        h="view_thistle")
    e.f(P, "viewing_kestrel", "818 Kestrel Avenue, Monday March 23 at 10:00", h="view_kestrel")

    e = w.ev("03-22 13:00", "voice",
             "After the Thistle Court viewing. No yard, small kitchen. Owen (bystander) liked the open kitchen. "
             "Joana, tentatively: 'maybe we apply for Thistle as a backup, I don't know'.", J)
    e.f("p001", "opinion_thistle", "Owen liked Thistle Court's open kitchen", by="p001", h="owen_thistle")
    e.f(P, "tentative_backup_application", "Maybe apply for Thistle Court as a backup (not decided)",
        h="thistle_backup", notes="tentative plan; dropped when Kestrel was approved")

    e = w.ev("03-23 11:30", "voice",
             "After the Kestrel viewing, walking back to the car. Joana loves it: fenced yard, 12-minute walk to "
             "Fernhill Elementary, gas range for Owen. 'Let's apply today.' Owen (bystander) still says Thistle was "
             "nicer inside.", J)
    e.f("house.kestrel", "walk_to_school", "12-minute walk to Fernhill Elementary", h="kestrel_walk")
    e.f(P, "decision_apply_kestrel", "Applying for 818 Kestrel Avenue", h="apply_kestrel")
    e.f("p001", "opinion_kestrel", "Owen thinks Thistle Court was nicer inside", by="p001", h="owen_kestrel_op",
        notes="others' opinion; the principal prefers Kestrel")

    e = w.ev("03-25 09:10", "inbound_email",
             "Email from Priya Sandoval: application received (45 dollar fee paid); please send two recent pay stubs "
             "by Friday March 27.", J)
    e.f(P, "task_send_pay_stubs", "Send Priya two recent pay stubs by Friday March 27", by="p023",
        sens=["private_message"], h="paystubs")

    e = w.ev("03-27 12:30", "typed",
             "Joana sends the pay stubs to Priya and asks the assistant to mark it done.", J)
    e.end("paystubs", "task completed")

    e = w.ev("04-02 16:40", "inbound_email",
             "Email from Priya: approved. Lease June 1, 2026 to May 31, 2027; rent 2,875; deposit 2,875; keys "
             "available from May 29.", J)
    e.f(P, "kestrel_lease_terms", "June 1, 2026 to May 31, 2027; rent 2,875; deposit 2,875", by="p023", sens=PMF,
        h="lease_terms")
    e.f(P, "kestrel_keys_available", "From May 29", by="p023", sens=["private_message"], h="keys_date")
    e.end("thistle_backup", "plan dropped (Kestrel approved)")

    e = w.ev("04-04 10:00", "typed",
             "Lease signed. Joana sets up rent autopay and types the checking account number to make sure she has it "
             "right ('Columbia Ridge checking 7731045892'). Rent at Kestrel will be 2,875 from June.", J)
    e.f("p023", "relationship_to_principal", "Landlord at 818 Kestrel Avenue (lease signed April 4)",
        sup="priya_rel0", h="priya_rel1", notes="relationship change")
    e.f(P, "checking_account_number", "7731045892 (Columbia Ridge Credit Union)", sens=["secret", "finance"],
        h="acct")
    e.f(P, "upcoming_rent", "2,875 dollars a month at Kestrel from June", sens=FIN, h="rent_next")
    e.f(P, "lease_signed", "Signed the Kestrel Avenue lease on April 4", h="lease_signed")

    e = w.ev("04-04 18:30", "inbound_text",
             "Gerald Pike acknowledges the notice: last day at Sorrel Street is May 31.", J)
    e.f(P, "sorrel_move_out_deadline", "May 31", by="p022", sens=["private_message"], h="moveout0")

    e = w.ev("04-08 14:00", "inbound_email",
             "Quote from Corinne Abara at Bluebird Movers: 2,350 dollars for Saturday May 30, 3 movers, about 6 "
             "hours, boxes extra.", J)
    e.f("p025", "relationship_to_principal", "Sales rep at Bluebird Movers (quoted the move)", by="p025",
        h="corinne_rel")
    e.f(P, "mover_quote_bluebird", "Bluebird Movers: 2,350 dollars for May 30, boxes extra", by="p025", sens=PMF,
        h="q_bluebird")

    e = w.ev("04-10 09:30", "inbound_email",
             "Quote from Dale Hartline, owner of Hartline Moving: 1,980 dollars for Saturday May 30, includes 20 "
             "boxes; 300 dollar deposit through the invoice on Hartline's own customer portal.", J)
    e.f("p024", "relationship_to_principal", "Owner of Hartline Moving", by="p024", h="dale_rel")
    e.f(P, "mover_quote_hartline", "Hartline Moving: 1,980 dollars for May 30, 20 boxes included", by="p024",
        sens=PMF, h="q_hartline")

    e = w.ev("04-12 19:00", "typed",
             "Joana chooses Hartline (cheaper, boxes included). Deposit of 300 due by April 24 through Hartline's "
             "invoice. Move date: Saturday May 30. The assistant points out the new rent is 725 a month more than the "
             "current one; Joana says 'ouch, but worth it'.", J)
    e.f(P, "mover", "Hartline Moving", h="mover")
    e.f(P, "move_date", "Saturday May 30", h="movedate0")
    e.f(P, "task_mover_deposit", "Pay Hartline's 300 dollar deposit through their invoice by April 24",
        sens=FIN, h="mover_dep")
    e.f(P, "rent_increase_computed", "The new rent is 725 dollars a month more than the Sorrel Street rent",
        by="assistant", sens=FIN, h="rent_diff", notes="assistant-provided figure the principal later refers to")

    e = w.ev("04-20 17:00", "voice",
             "Walking Waffles. Neighbour Agnes Whitcombe stopped her: Agnes says Gerald is selling the building. "
             "Joana: 'no idea if that's true'.", J)
    e.f("p029", "relationship_to_principal", "Elderly neighbour at Sorrel Street", h="agnes_rel")
    e.f("p022", "selling_rumour", "Agnes says Gerald Pike is selling the Sorrel Street building", by="p029",
        rep=True, h="gerald_selling_rumour", notes="reported claim; confirmed by Gerald on May 9")

    e = w.ev("04-23 08:45", "typed",
             "Paid Hartline's 300 deposit through the invoice link in Dale's original email.", J)
    e.end("mover_dep", "task completed")
    e.f(P, "mover_deposit_paid", "Paid 300 dollar deposit to Hartline Moving on April 23", sens=FIN, h="dep_paid")

    e = w.ev("05-02 10:00", "typed",
             "Shopping list for the new house, first items: blackout curtains for Lucas's room, a doormat, shelf "
             "brackets, a garden hose. Owen wants a blue couch; Joana wants green.", J)
    for i, item in enumerate(["Blackout curtains for Lucas's room", "Doormat", "Shelf brackets", "Garden hose"]):
        e.f(P, "new_house_shopping_list", item, h=f"hl{i}", multi=True)
    e.f("p001", "couch_preference", "Owen wants a blue couch", by="p001", h="owen_blue")
    e.f(P, "couch_preference", "Wants a green couch", h="jo_green")

    e = w.ev("05-08 13:20", "inbound_email",
             "Dale Hartline: their big truck is in the shop; they cannot do May 30. Offers Saturday June 6, same "
             "price.", J)
    e.f("p024", "reschedule_offer", "Cannot move them on May 30; offers Saturday June 6 at the same price",
        by="p024", sens=["private_message"], h="dale_resched")

    e = w.ev("05-09 11:00", "inbound_text",
             "Gerald Pike agrees they can stay until June 7 for a prorated 500 dollars. Adds, 'between us, I am "
             "selling the building this fall'.", J)
    e.f("p022", "stay_extension", "Can stay at Sorrel Street until June 7 for a prorated 500 dollars", by="p022",
        sens=PMF, h="extension")
    e.f(P, "sorrel_move_out_deadline", "June 7 (extended, prorated 500 dollars)", by="p022", sup="moveout0",
        sens=["private_message"], h="moveout1")
    e.f("p022", "selling_building", "Gerald is selling the Sorrel Street building this fall", by="p022",
        sens=["private_message", "third_party"], h="gerald_selling", notes="confirms Agnes's rumour")

    e = w.ev("05-09 19:30", "typed",
             "Joana confirms with Hartline: the move is now Saturday June 6, crew arrives at 8:00.", J)
    e.f(P, "move_date", "Saturday June 6, Hartline crew arrives at 8:00", sup="movedate0", h="movedate1")

    e = w.ev("05-18 16:00", "voice",
             "At Northgate Self Storage renting a small overflow unit for three months. She says the unit number "
             "(112) and gate code (zero five two five) aloud; storage is 89 a month.", J)
    e.f(P, "storage_unit", "Northgate Self Storage, unit 112, 89 dollars a month", sens=FIN, h="storage")
    e.f(P, "storage_gate_code", "0525", sens=["secret"], h="storagecode", notes="secret said by voice")

    e = w.ev("05-18 20:30", "typed",
             "Adds to the new-house list: a second bookshelf and a bike rack for the garage. Crosses off the doormat "
             "(Agnes gave them one).", J)
    e.f(P, "new_house_shopping_list", "Second bookshelf", h="hl4", multi=True)
    e.f(P, "new_house_shopping_list", "Bike rack for the garage", h="hl5", multi=True)
    e.end("hl1", "removed from the list (got one as a gift)")

    e = w.ev("05-20 18:00", "typed",
             "Internet: Cascadia Fiber install at Kestrel on Friday June 5 between 8 and 10 am; 65 a month.", J)
    e.f(P, "internet_install", "Cascadia Fiber, Friday June 5, 8 to 10 am", h="fiber")
    e.f(P, "internet_plan", "Cascadia Fiber, 65 dollars a month", h="fiber_plan")

    e = w.ev("05-26 12:15", "remote",
             "From the branch on her phone, typo-heavy: change-of-address to-dos: bank, DMV, Lucas's school, car "
             "insurance, USPS forwarding.", J)
    e.f(P, "address_change_bank", "Pending: update address with Columbia Ridge Credit Union", h="ac_bank0")
    e.f(P, "address_change_dmv", "Pending: update address with the DMV", h="ac_dmv0")
    e.f(P, "address_change_school", "Pending: give Fernhill Elementary the new address", h="ac_school0")
    e.f(P, "address_change_usps", "Pending: set up USPS mail forwarding", h="ac_usps0")

    e = w.ev("05-29 17:30", "voice",
             "Picked up the Kestrel keys from Priya. Priya told her trash and recycling go out Wednesday nights for "
             "Thursday pickup. Joana, cheerful, on speaker in the car.", J)
    e.f("house.kestrel", "trash_day", "Bins out Wednesday night for Thursday pickup", by="p023", h="trash")
    e.f(P, "kestrel_keys", "Picked up the Kestrel keys from Priya on May 29", h="keys_got")

    e = w.ev("06-01 09:30", "typed",
             "USPS forwarding set up from June 6. Fernhill Elementary has the new address too.", J)
    e.f(P, "address_change_usps", "Done: USPS forwarding from June 6", sup="ac_usps0", h="ac_usps1")
    e.f(P, "address_change_school", "Done: Fernhill Elementary has the new address", sup="ac_school0", h="ac_school1")

    e = w.ev("06-06 17:40", "voice",
             "Moving day evening at Kestrel. Hartline arrived at 8:05 and finished at 3. 'We're officially in.' "
             "Lucas (bystander) wants the attic room as his 'volcano lab'. Owen (bystander): 'the gas stove is "
             "beautiful'.", J)
    e.f(P, "home_address", "818 Kestrel Avenue, Portland (rented house)", sup="addr0", h="addr1")
    e.f(P, "move_completed", "Moved into 818 Kestrel Avenue on Saturday June 6", h="moved")
    e.f(P, "rent", "2,875 dollars a month at 818 Kestrel Avenue", sens=FIN, sup="rent0", h="rent1")
    e.end("rent_next", "the new rent is now the current rent")
    e.end("movedate1", "the move happened")

    e = w.ev("06-08 21:00", "typed",
             "'Remember: at Kestrel the recycling and trash go out Wednesday night.' The couch decision: they "
             "compromised on a teal couch from Fernwood Furniture, delivery Saturday June 20.", J)
    e.f(P, "remember_trash_night", "Recycling and trash go out Wednesday night at Kestrel", h="trash_rem",
        notes="explicit remember; duplicates Priya's info with the same content")
    e.f(P, "couch", "Teal couch from Fernwood Furniture, delivery Saturday June 20", h="couch")
    e.end("jo_green", "preference resolved by the teal couch compromise")
    e.end("owen_blue", "preference resolved by the teal couch compromise")

    e = w.ev("06-10 08:30", "typed",
             "Updated the address with the credit union. 'Also forget the old Sorrel Street door code, we're out.'", J)
    e.f(P, "address_change_bank", "Done: Columbia Ridge has the new address", sup="ac_bank0", h="ac_bank1")
    e.forget(["doorcode"])

    e = w.ev("06-13 11:00", "typed",
             "Borrowed Walt's ladder to hang curtains; promised to return it by Sunday June 21.", J)
    e.f(P, "promise_return_ladder", "Return Walt's ladder by Sunday June 21", h="ladder")

    e = w.ev("06-17 10:00", "inbound_email",
             "Gerald Pike: move-out inspection done; the deposit will be returned by July 7 with an itemised list.", J)
    e.f(P, "sorrel_deposit_return_due", "By July 7", by="p022", sens=PMF, h="dep_due")

    e = w.ev("06-20 18:00", "typed",
             "Teal couch delivered. Returned Walt's ladder with a six-pack of root beer.", J)
    e.end("ladder", "promise kept")
    e.f(P, "couch_delivered", "Teal couch delivered on June 20", h="couch_done")

    e = w.ev("06-24 13:00", "typed",
             "DMV address change finally went through online.", J)
    e.f(P, "address_change_dmv", "Done: DMV has the new address (June 24)", sup="ac_dmv0", h="ac_dmv1")

    e = w.ev("07-06 18:30", "inbound_email",
             "Gerald Pike: returning 1,900 of the 2,150 deposit; 250 kept for carpet cleaning.", J)
    e.f(P, "sorrel_deposit_returned", "1,900 of 2,150 returned; 250 kept for carpet cleaning", by="p022", sens=PMF,
        h="dep0")
    e.end("dep_due", "deposit returned")

    e = w.ev("07-07 20:00", "typed",
             "Joana drafts a reply disputing the 250: the carpet was nine years old, normal wear. Asks for a firm but "
             "polite tone.", J)
    e.f(P, "deposit_dispute", "Disputing the 250 carpet charge (carpet nine years old, normal wear)", sens=FIN,
        h="dispute")

    e = w.ev("07-20 09:00", "inbound_email",
             "Gerald (read in Brazil): refunds another 150, keeps 100. Total returned 2,050. Joana replies 'fine, "
             "done'.", J)
    e.f(P, "sorrel_deposit_returned", "2,050 of 2,150 returned in total (another 150 on July 20); 100 kept",
        by="p022", sens=PMF, sup="dep0", h="dep1")
    e.end("dispute", "dispute settled")

    e = w.ev("08-18 17:30", "remote",
             "Quick message from the storage place: emptied and closed the overflow unit, last box went to the "
             "garage. Typos.", J)
    e.end("storage", "storage unit closed")
    e.end("storagecode", "storage unit closed; code no longer in use")
