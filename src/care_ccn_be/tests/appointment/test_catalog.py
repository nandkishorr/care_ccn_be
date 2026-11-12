from care_ccn_be.resources.catalog.appointment import AppointmentCatalogBuilder
from care_ccn_be.tests.appointment.base import AppointmentTestBase


class CatalogTests(AppointmentTestBase):
    def catalogs(self, public_only=True):
        from care_ccn_be.services.publisher import PublishSpec

        return AppointmentCatalogBuilder().catalogs(PublishSpec(public_only=public_only))

    def test_a_catalog_describes_the_facility_and_its_resources(self):
        (catalog,) = self.catalogs()
        self.assertEqual(catalog["id"], f"catalog-{self.facility.external_id}")
        self.assertEqual(catalog["descriptor"], {"name": "City Clinic"})
        self.assertEqual(catalog["provider"]["id"], str(self.facility.external_id))
        self.assertEqual((catalog["bppId"], catalog["bapId"]), ("bpp.care", "bap.care"))
        (resource,) = catalog["resources"]
        self.assertEqual(resource["id"], str(self.resource.external_id))
        self.assertEqual(resource["descriptor"]["name"], "Anita Rao")
        attributes = resource["resourceAttributes"]
        self.assertEqual(attributes["healthServiceType"], "PHYSICAL_CONSULTATION")
        self.assertEqual(attributes["acceptanceMode"], "AUTO")
        weekday = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")[self.day.weekday()]
        self.assertEqual(
            attributes["availabilitySchedule"], [{"dayOfWeek": weekday, "startTime": "09:00", "endTime": "11:00"}]
        )
        (offer,) = catalog["offers"]
        self.assertEqual(offer["id"], f"offer-{self.resource.external_id}")
        self.assertEqual(offer["resourceIds"], [str(self.resource.external_id)])
        self.assertEqual(offer["offerAttributes"]["healthServiceType"], "PHYSICAL_CONSULTATION")

    def test_build_returns_the_first_catalog_or_nothing(self):
        builder = AppointmentCatalogBuilder()
        self.assertEqual(builder.build()["id"], f"catalog-{self.facility.external_id}")
        self.schedule.is_public = False
        self.schedule.save()
        self.assertEqual(builder.build(), {})
        self.assertEqual(builder.catalogs(), [])

    def test_schedule_meta_overrides_the_service_type_and_acceptance_mode(self):
        self.schedule.meta = {"beckn": {"healthServiceType": "TELE_CONSULTATION", "acceptanceMode": "MANUAL_REVIEW"}}
        self.schedule.save()
        (catalog,) = self.catalogs()
        attributes = catalog["resources"][0]["resourceAttributes"]
        self.assertEqual(
            (attributes["healthServiceType"], attributes["acceptanceMode"]), ("TELE_CONSULTATION", "MANUAL_REVIEW")
        )
        self.assertEqual(catalog["offers"][0]["offerAttributes"]["healthServiceType"], "TELE_CONSULTATION")

    def test_schedule_meta_can_point_to_another_bpp(self):
        self.schedule.meta = {"beckn": {"bppId": "bpp.provider", "bppUri": "http://provider"}}
        self.schedule.save()
        (catalog,) = self.catalogs()
        self.assertEqual((catalog["bppId"], catalog["bppUri"]), ("bpp.provider", "http://provider"))

    def test_every_facility_gets_its_own_catalog(self):
        other_facility = self.create_facility(self.user, name="Town Clinic")
        other_resource = self.make_resource(self.create_user(username="dr.town"))
        other_resource.facility = other_facility
        other_resource.save()
        self.make_schedule(other_resource)
        catalogs = self.catalogs()
        self.assertEqual(
            {c["id"] for c in catalogs},
            {f"catalog-{self.facility.external_id}", f"catalog-{other_facility.external_id}"},
        )

    def test_private_schedules_are_left_out_unless_requested(self):
        private = self.make_resource(self.create_user(username="dr.private"))
        self.make_schedule(private, is_public=False)
        self.assertEqual([r["id"] for r in self.catalogs()[0]["resources"]], [str(self.resource.external_id)])
        self.assertEqual(len(self.catalogs(public_only=False)[0]["resources"]), 2)

    def test_a_resource_with_two_schedules_is_listed_once(self):
        self.make_schedule(self.resource, name="Evening OPD")
        (catalog,) = self.catalogs()
        self.assertEqual(len(catalog["resources"]), 1)
        self.assertEqual(len(catalog["resources"][0]["resourceAttributes"]["availabilitySchedule"]), 2)
