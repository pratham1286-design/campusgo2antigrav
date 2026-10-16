"""Sign up and log in with SMS one-time passwords."""
import os
import unittest
from unittest import mock

from test_support import SMS_OUTBOX, ClientMixin, LPU_IDS, PHONES, last_otp
import emergency_dispatch
import shared

REVIEW_ENV = {"REVIEW_LOGIN_PHONE": "9123499999", "REVIEW_LOGIN_CODE": "246810"}


def wrong_code(real):
    return "000000" if real != "000000" else "111111"


class TestSignupAndLogin(ClientMixin, unittest.TestCase):
    def start_signup(self, **overrides):
        shared.clear_rate_limits()
        payload = {"name": "Nisha Verma", "username": "nisha.verma", "user_type": "student",
                   "lpu_id": "12400001", "phone": "+91 91234 56780"}
        payload.update(overrides)
        return self.client.post("/api/auth/signup/start", json=payload)

    def test_suggestions_skip_taken_usernames(self):
        shared.clear_rate_limits()
        res = self.client.post("/api/auth/username-suggestions", json={"name": "Aarav Mehta"})
        names = res.get_json()["suggestions"]
        self.assertEqual(len(names), 5)
        self.assertNotIn("aarav.mehta", names)  # the fixture user already owns it
        self.assertEqual(len(set(names)), 5)
        for n in names:
            available = self.client.post("/api/auth/username-available", json={"username": n}).get_json()
            self.assertTrue(available["available"], n)

    def test_full_signup_then_login_keeps_data(self):
        res = self.start_signup()
        self.assertEqual(res.status_code, 200, res.get_json())
        start = res.get_json()
        self.assertNotIn("demo_otp", start)
        code = last_otp("9123456780")

        wrong = self.client.post("/api/auth/signup/verify",
                                 json={"challenge_id": start["challenge_id"], "otp": wrong_code(code)})
        self.assertEqual(wrong.status_code, 400)

        done = self.client.post("/api/auth/signup/verify", json={"challenge_id": start["challenge_id"], "otp": code})
        self.assertEqual(done.status_code, 200, done.get_json())
        body = done.get_json()
        self.assertEqual(body["user"]["username"], "nisha.verma")
        self.assertEqual(body["user"]["lpu_id"], "12400001")
        headers = {"Authorization": f"Bearer {body['token']}"}
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)

        replay = self.client.post("/api/auth/signup/verify", json={"challenge_id": start["challenge_id"], "otp": code})
        self.assertEqual(replay.status_code, 400)

        # A fresh install logs in with LPU ID + mobile + OTP and gets the same account back.
        shared.clear_rate_limits()
        self.db("DELETE FROM otp_challenges")
        self.client.post("/api/user/role", headers=headers, json={"role": "both"})
        login = self.client.post("/api/auth/login/start", json={"lpu_id": "12400001", "phone": "9123456780"}).get_json()
        again = self.client.post("/api/auth/login/verify",
                                 json={"challenge_id": login["challenge_id"], "otp": last_otp("9123456780")}).get_json()
        self.assertEqual(again["user"]["id"], body["user"]["id"])
        self.assertEqual(again["user"]["role"], "both")

    def test_signup_rejects_duplicates_and_bad_input(self):
        self.assertEqual(self.start_signup(username="aarav.mehta", lpu_id="12400002", phone="9123400002").status_code, 409)
        self.assertEqual(self.start_signup(username="x1x1x1", lpu_id=LPU_IDS["usr_student_aarav"], phone="9123400003").status_code, 409)
        self.assertEqual(self.start_signup(username="x2x2x2", lpu_id="12400004", phone=PHONES["usr_student_aarav"]).status_code, 409)
        self.assertEqual(self.start_signup(lpu_id="123").status_code, 400)
        self.assertEqual(self.start_signup(phone="12345").status_code, 400)
        self.assertEqual(self.start_signup(name="").status_code, 400)
        self.assertEqual(self.start_signup(username="Bad Name!").status_code, 400)
        self.assertEqual(self.start_signup(user_type="admin").status_code, 400)

    def test_teacher_signup(self):
        start = self.start_signup(name="Meera Iyer", username="meera.iyer", user_type="teacher",
                                  lpu_id="fac-20001", phone="9123400005").get_json()
        user = self.client.post("/api/auth/signup/verify",
                                json={"challenge_id": start["challenge_id"], "otp": last_otp("9123400005")}).get_json()["user"]
        self.assertEqual(user["lpu_id"], "FAC-20001")
        self.assertEqual(user["user_type"], "teacher")

    def test_login_with_the_wrong_number_texts_nobody(self):
        shared.clear_rate_limits()
        res = self.client.post("/api/auth/login/start",
                               json={"lpu_id": LPU_IDS["usr_student_aarav"], "phone": PHONES["usr_student_kavya"]})
        # The person is told plainly (sign-up already says which numbers have accounts), and the
        # number typed by mistake gets no text.
        self.assertEqual((res.status_code, res.get_json()["code"]), (404, "ACCOUNT_NOT_FOUND"))
        self.assertEqual(SMS_OUTBOX, [])

    def test_wrong_code_locks_challenge_after_five_tries(self):
        shared.clear_rate_limits()
        start = self.client.post("/api/auth/login/start",
                                 json={"lpu_id": LPU_IDS["usr_driver_vikram"], "phone": PHONES["usr_driver_vikram"]}).get_json()
        code = last_otp(PHONES["usr_driver_vikram"])
        for _ in range(5):
            shared.clear_rate_limits()
            res = self.client.post("/api/auth/login/verify",
                                   json={"challenge_id": start["challenge_id"], "otp": wrong_code(code)})
            self.assertEqual(res.status_code, 400)
        shared.clear_rate_limits()
        right = self.client.post("/api/auth/login/verify", json={"challenge_id": start["challenge_id"], "otp": code})
        self.assertEqual(right.status_code, 429)

    def test_signup_code_cannot_be_used_to_log_in(self):
        start = self.start_signup(username="cross.check", lpu_id="12400006", phone="9123400006").get_json()
        res = self.client.post("/api/auth/login/verify",
                               json={"challenge_id": start["challenge_id"], "otp": last_otp("9123400006")})
        self.assertEqual(res.status_code, 400)

    def test_resend_cooldown(self):
        shared.clear_rate_limits()
        payload = {"lpu_id": LPU_IDS["usr_driver_simran"], "phone": PHONES["usr_driver_simran"]}
        self.assertEqual(self.client.post("/api/auth/login/start", json=payload).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/login/start", json=payload).status_code, 429)

    def test_without_an_sms_gateway_no_code_is_issued(self):
        shared.clear_rate_limits()
        with mock.patch.object(emergency_dispatch, "FAST2SMS_API_KEY", None):
            res = self.client.post("/api/auth/login/start",
                                   json={"lpu_id": LPU_IDS["usr_student_kavya"], "phone": PHONES["usr_student_kavya"]})
        self.assertEqual(res.status_code, 503)

    def test_old_password_login_is_gone(self):
        self.assertIn(self.client.post("/api/auth/login", json={"lpu_id": "12204592", "password": "x"}).status_code, (404, 405))


class TestReviewLogin(ClientMixin, unittest.TestCase):
    """App Store / Play reviewers sign in with a fixed code on one configured number."""

    def test_review_number_signs_up_and_logs_in_with_the_fixed_code_and_no_sms(self):
        with mock.patch.dict(os.environ, REVIEW_ENV), mock.patch.object(emergency_dispatch, "FAST2SMS_API_KEY", None):
            start = self.client.post("/api/auth/signup/start", json={
                "name": "App Review", "username": "app.review", "user_type": "student",
                "lpu_id": "12499999", "phone": "9123499999"})
            self.assertEqual(start.status_code, 200, start.get_json())
            done = self.client.post("/api/auth/signup/verify",
                                    json={"challenge_id": start.get_json()["challenge_id"], "otp": "246810"})
            self.assertEqual(done.status_code, 200, done.get_json())
            self.assertEqual(SMS_OUTBOX, [])

            # Every other number still needs a real SMS, which isn't configured here.
            other = self.client.post("/api/auth/login/start",
                                     json={"lpu_id": LPU_IDS["usr_student_kavya"], "phone": PHONES["usr_student_kavya"]})
            self.assertEqual(other.status_code, 503)

    def test_review_code_is_ignored_unless_it_is_six_digits(self):
        with mock.patch.dict(os.environ, {**REVIEW_ENV, "REVIEW_LOGIN_CODE": "12ab"}):
            self.client.post("/api/auth/signup/start", json={
                "name": "App Review", "username": "app.review2", "user_type": "student",
                "lpu_id": "12499998", "phone": "9123499999"})
        self.assertRegex(last_otp("9123499999"), r"^\d{6}$")  # a normal random code was texted


if __name__ == "__main__":
    unittest.main()
