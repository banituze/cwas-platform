"""Legal texts. English is the source; Malagasy and French live in translations.py.
These are working drafts for the Ampotaka pilot and should be reviewed by a qualified lawyer before launch."""
UPDATED = "2026-09-20"

DOCS = {
    "terms": {
        "title": "Terms of Service",
        "intro": "These terms govern your use of CWAS, the Community Water Access Scheduler run for the households of Ampotaka, Madagascar. By creating an account or using the service on the web, by USSD or by SMS, you agree to them.",
        "sections": [
            ("Who can use CWAS", ["CWAS is for households, community coordinators and administrators taking part in the Ampotaka pilot. You must give accurate details, and be an adult or use the service through an adult of your household.",
                                  "Coordinator and administrator accounts need an approval or an enrollment code, and can be removed at any time."]),
            ("Your account", ["Keep your password and PIN private. Everything done with them is treated as done by you. Tell your coordinator at once if you think someone else used them.",
                              "We may lock an account after repeated wrong passwords or PINs to protect it."]),
            ("Booking water", ["A booking reserves a time slot at a water point. It is confirmed only when a coordinator approves it, or automatically when the system settings allow. Slots depend on opening hours, capacity and maintenance and can change.",
                               "Each household may hold one active booking per day. The litres, the price and any subsidy are shown before you confirm."]),
            ("Payments and your wallet", ["You add money to your wallet by Orange Money, Airtel Money or cash through an agent. The price of a booking is taken from the wallet when you confirm. A mobile-money deposit is credited once the provider confirms it.",
                                          "The wallet is a prepayment balance for water. It is not a bank account, earns no interest and cannot be transferred to another household."]),
            ("Cancellations, no-shows and maintenance", ["You can cancel a future booking before its slot starts and the money returns to your wallet once. If a coordinator denies a booking, or maintenance or a closure cancels it, you are refunded automatically.",
                                                         "Approved bookings that are not collected can be marked as no-shows, and repeated no-shows can lower your fair-share score. See the Refund Policy for details."]),
            ("Fair use and priority", ["Water is shared. Coordinators set priority levels from need: vulnerability, household size, distance and recent use. Do not give false information, resell water or book more than your household needs.",
                                       "Automated checks look for unusual patterns such as repeated cancellations or sudden large deposits. Coordinators review them, and every decision can be explained."]),
            ("Acceptable use", ["Do not try to break, overload or bypass the service, use another person's account, or send abusive or unlawful content through the assistant, announcements or messages."]),
            ("Availability", ["The pilot depends on mobile networks and on water infrastructure that can fail. We work for a reliable service but cannot promise uninterrupted access, and CWAS does not control the water supply itself."]),
            ("Changes and ending", ["We may update these terms. The date at the top shows the latest version and important changes are announced in the app. You can delete your account at any time from your profile. We may suspend accounts that break these terms."]),
            ("Contact", ["Ask your community coordinator, or write to info@winebald.tech."]),
        ],
    },
    "privacy": {
        "title": "Privacy Policy",
        "intro": "This policy explains what CWAS collects, why, who can see it and the choices you have.",
        "sections": [
            ("What we collect", ["Account details (name, phone, email, language), household details (village, address, household size, access needs), wallet and booking records, notifications, assistant chats and the files you upload, and technical logs (time, channel and IP address, kept for security)."]),
            ("Why we use it", ["To run bookings and payments, to prioritise fairly, to send confirmations by app and SMS, to keep the service secure, and to produce anonymous usage and equity reports for the community water committee."]),
            ("Who can see it", ["Coordinators see the households they serve. Administrators see accounts and the audit trail. Telecom and mobile-money providers process the messages and payments sent through them.",
                                "We do not sell personal data and we do not use it for advertising."]),
            ("Automated suggestions", ["CWAS suggests priorities, approvals and warnings using simple rules applied to household data. A coordinator makes the final decision, and every point of a score has a stated reason."]),
            ("Security", ["Passwords and PINs are stored only as one-way hashes. Sessions use secure cookies, important actions go into a tamper-evident audit trail, and access depends on role. No system is perfectly secure, so tell us at once if you suspect misuse."]),
            ("How long we keep data", ["Account and household details are kept while your account is active. Booking, wallet and audit records are kept for accounting and security after you leave; your name and contact details are removed from your account. Chats and uploaded files are deleted when you delete them or your account."]),
            ("Your choices and rights", ["You can see and correct your details in your profile, download a copy of your data, delete your chats and delete your account. For anything else, ask your coordinator or write to info@winebald.tech."]),
            ("Cookies and device storage", ["We use essential cookies for sign-in, language and theme, and your browser stores your sound and voice choices. We use no advertising or tracking cookies."]),
            ("Household needs", ["The needs you tick at registration, such as an older person or a disability in the home, and your distance to water are used only to set priority and subsidies. A coordinator checks them before any discount applies, and you can change them in your profile at any time."]),
            ("Pilot news", ["If you follow the pilot, we keep only your email address and language. We use them only to announce milestones, never share them, and delete them as soon as you use the leave link."]),
            ("Children", ["CWAS is for adults in a household. Do not register children as account holders."]),
            ("Changes and contact", ["Important changes are announced in the app. Contact info@winebald.tech with any question about your data."]),
        ],
    },
    "refunds": {
        "title": "Refund Policy",
        "intro": "This policy says when money returns to your wallet and how to get unused money back.",
        "sections": [
            ("Automatic refunds", ["The price of a booking returns to your wallet automatically, once, when you cancel before the slot starts, when a coordinator denies it, when it expires without review, or when maintenance or a closure cancels it."]),
            ("When refunds do not apply", ["There is no refund for a booking that was approved and collected, or marked as a no-show because nobody came, unless a coordinator decides otherwise for a good reason."]),
            ("How to cancel", ["You can cancel a pending or approved booking until its slot starts: on the web, by USSD (Cancel booking) or by SMS (CANCEL followed by the booking reference)."]),
            ("Failed or wrong deposits", ["If a mobile-money payment was taken but your wallet was not credited, keep the transaction message and tell your coordinator the reference. We check with the provider and credit or return the amount, normally within 3 working days."]),
            ("Unused wallet balance", ["You can ask your coordinator to pay back unused wallet money in cash. Requests are checked against your ledger and normally paid within 7 days. Fees charged by mobile-money providers cannot be refunded by CWAS."]),
            ("Closing your account", ["When you delete your account, any remaining balance is recorded as a refund due and your coordinator is told, so it can be paid back to you under this policy."]),
            ("Water supply problems", ["If a water point is closed or unusable, your booking is cancelled and refunded automatically. CWAS does not control the water itself."]),
            ("Disputes", ["If you disagree with a decision, ask your coordinator to review it. Administrators can check the audit trail, and every refund decision is recorded."]),
            ("Contact", ["Ask your community coordinator, or write to info@winebald.tech."]),
        ],
    },
}
