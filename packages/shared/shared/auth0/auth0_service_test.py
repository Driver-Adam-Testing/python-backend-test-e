import unittest
from unittest.mock import patch

import pytest
from database.models_enums import OrgRole

from shared.auth0.auth0_service import Auth0Service
from shared.auth0.models import User as UserToken
from shared.auth0.schemas import (
    CreateInvitationInput,
    Invitation,
    Invitee,
    ModifyUserRolesResponse,
)

# Test constants
MOCK_MGMT_API_DOMAIN = "mock_mgmt_api_domain"
MOCK_MGMT_API_CLIENT_ID = "mock_mgmt_api_client_id"
MOCK_MGMT_API_CLIENT_SECRET = "mock_mgmt_api_client_secret"
MOCK_AUTH0_DOMAIN = "mock_auth0_domain"
MOCK_AUTH0_CLIENT_ID = "mock_auth0_client_id"


def create_test_auth0_service() -> Auth0Service:
    """Helper to create Auth0Service with test credentials."""
    return Auth0Service(
        auth0_mgmt_domain=MOCK_MGMT_API_DOMAIN,
        auth0_mgmt_client_id=MOCK_MGMT_API_CLIENT_ID,
        auth0_mgmt_client_secret=MOCK_MGMT_API_CLIENT_SECRET,
        auth0_domain=MOCK_AUTH0_DOMAIN,
        auth0_client_id=MOCK_AUTH0_CLIENT_ID,
    )


@pytest.fixture(autouse=True)
def mock_get_token() -> any:
    with patch("auth0.authentication.GetToken.client_credentials") as mock_get_token:
        mock_get_token.return_value = {
            "access_token": "mocked_access_token",
            "id_token": "mocked_id_token",
        }
        yield


class TestAuth0Service(unittest.TestCase):
    @patch("auth0.authentication.GetToken.client_credentials")
    def test_get_mgmt_api_token(self, mock_get_token: any) -> None:
        mock_get_token.return_value = {
            "access_token": "mocked_access_token",
            "id_token": "mocked_id_token",
        }
        auth0_service = create_test_auth0_service()
        response = auth0_service.get_mgmt_api_token()
        self.assertTrue(mock_get_token.called)
        mock_get_token.assert_called_with("https://mock_mgmt_api_domain/api/v2/")
        self.assertEqual(response, "mocked_access_token")

    @patch("auth0.authentication.Users.userinfo")
    @patch("auth0.authentication.Database.change_password")
    @patch("auth0.authentication.Database.post")
    def test_change_self_password(
        self, mock_database_login: any, mock_change_password: any, mock_userinfo: any
    ) -> None:
        mock_userinfo.return_value = {
            "email": "mock@email.com",
        }
        mock_change_password.return_value = {"mocked": "response"}
        mock_database_login.return_value = {}
        auth0_service = create_test_auth0_service()
        response = auth0_service.change_self_password(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=[],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            "mock_user_access_token",
        )
        self.assertTrue(response["mocked"] == "response")
        mock_userinfo.assert_called_with("mock_user_access_token")
        mock_change_password.assert_called_with(
            email="mock@email.com",
            connection="Username-Password-Authentication",
            organization="mock_org_id",
        )

    @patch("auth0.management.Users.list_organizations")
    def test_list_user_organizations(self, mock_list_organizations: any) -> None:
        mock_list_organizations.return_value = {"mocked": "orgs"}
        auth0_service = create_test_auth0_service()
        response = auth0_service.list_user_organizations(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=[],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            )
        )
        self.assertTrue(response["mocked"] == "orgs")
        # Assume a user will not belong to more than 100 organizations at once.
        # If this needs to change, we can proxy page and per_page calls through too.
        mock_list_organizations.assert_called_with("mock_subject", per_page=100)

    @patch("auth0.management.Organizations.delete_organization_member_roles")
    @patch("auth0.management.Organizations.create_organization_member_roles")
    @patch("auth0.management.Organizations.all_organization_member_roles")
    def test_modify_user_roles(
        self,
        mock_org_member_roles: any,
        mock_create_member_roles: any,
        mock_delete_member_roles: any,
    ) -> None:
        mock_org_member_roles.return_value = []
        mock_create_member_roles.return_value = {}
        mock_delete_member_roles.return_value = {}

        auth0_service = create_test_auth0_service()
        response1 = auth0_service.modify_user_roles(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=["organization:management"],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            roles=["mockrole1", "mockrole2"],
            modified_user_id="mock_uid",
        )
        self.assertEqual(
            response1,
            ModifyUserRolesResponse(
                user_id="mock_uid",
                added_roles=["mockrole1", "mockrole2"],
                removed_roles=[],
            ),
        )

        # Test that correct roles are removed
        mock_org_member_roles.return_value = [{"id": "shouldberemoved"}]
        response2 = auth0_service.modify_user_roles(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=["organization:management"],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            roles=["mockrole1"],
            modified_user_id="mock_uid",
        )
        self.assertEqual(
            response2,
            ModifyUserRolesResponse(
                user_id="mock_uid",
                added_roles=["mockrole1"],
                removed_roles=["shouldberemoved"],
            ),
        )

    @patch("auth0.management.Organizations.all_organization_members")
    def test_list_members(self, mock_list_members: any) -> None:
        mock_list_members.return_value = {"mocked": "listed org members"}
        auth0_service = create_test_auth0_service()
        response = auth0_service.list_members(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=["organization:management"],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            page=1,
            per_page=9,
        )
        self.assertTrue(response["mocked"] == "listed org members")
        mock_list_members.assert_called_with(
            id="mock_org_id",
            page=1,
            per_page=9,
            fields=["user_id", "email", "picture", "name", "roles"],
        )

    @patch("auth0.management.Organizations.all_organization_invitations")
    def test_list_invitations(self, mock_all_org_invitations: any) -> None:
        mock_all_org_invitations.return_value = {"mocked": "org invitations"}
        auth0_service = create_test_auth0_service()
        response = auth0_service.list_invitations(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=["organization:management"],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            page=2,
            per_page=3,
        )
        self.assertTrue(response["mocked"] == "org invitations")
        mock_all_org_invitations.assert_called_with(
            id="mock_org_id", page=2, per_page=3
        )

    @patch("auth0.management.Roles.list")
    def test_list_roles(self, mock_list_roles: any) -> None:
        mock_list_roles.return_value = {"mocked": "list of roles"}
        auth0_service = create_test_auth0_service()
        response = auth0_service.list_roles()
        self.assertTrue(response["mocked"] == "list of roles")
        # Verify default paginations params are passed through
        mock_list_roles.assert_called_with(page=0, per_page=100)

    @patch("auth0.authentication.Users.userinfo")
    @patch("auth0.management.Organizations.get_organization")
    @patch("auth0.management.Organizations.create_organization_invitation")
    def test_create_invitation(
        self, mock_create_invitation: any, mock_get_org: any, mock_userinfo: any
    ) -> None:
        mock_create_invitation.return_value = {"invitation": "created"}
        mock_get_org.return_value = {"metadata": {}}
        mock_userinfo.return_value = {"name": "Johnny Cache"}
        auth0_service = create_test_auth0_service()
        response = auth0_service.create_invitation(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=["organization:management"],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            access_token="mock_user_access_token",
            invitations=CreateInvitationInput(
                invitations=[
                    Invitation(
                        invitee=Invitee(email="mock@invitation.com"),
                        role=OrgRole.org_member,
                    ),
                    Invitation(
                        invitee=Invitee(email="another@invitation.com"),
                        role=OrgRole.org_super_admin,
                    ),
                ]
            ),
        )
        self.assertEqual(2, mock_create_invitation.call_count)
        self.assertTrue(len(response) == 2)
        self.assertTrue(response[0]["invitation"] == "created")
        mock_create_invitation.assert_any_call(
            id="mock_org_id",
            body={
                "inviter": {"name": "Johnny Cache"},
                "invitee": {"email": "mock@invitation.com"},
                "client_id": "mock_auth0_client_id",
                "app_metadata": {"mock_org_id": {"initial_org_role": "org_member"}},
            },
        )

    @patch("auth0.management.Organizations.delete_organization_members")
    def test_delete_user_from_organization(self, mock_delete_members: any) -> None:
        mock_delete_members.return_value = {"member": "deleted"}
        auth0_service = create_test_auth0_service()
        response = auth0_service.delete_user_from_organization(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=["organization:management"],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            "mock_user_id_to_remove",
        )
        self.assertTrue(response["member"] == "deleted")
        mock_delete_members.assert_called_with(
            id="mock_org_id",
            body={"members": ["mock_user_id_to_remove"]},
        )

    @patch("auth0.management.Organizations.delete_organization_invitation")
    def test_delete_invitation(self, mock_delete_invitation: any) -> None:
        mock_delete_invitation.return_value = {"invitation": "deleted"}
        auth0_service = create_test_auth0_service()
        response = auth0_service.delete_invitation(
            UserToken(
                org_id="mock_org_id",
                sub="mock_subject",
                org_name="mock_org_name",
                iss="mock_issuer",
                aud="mock_audience",
                iat=1,
                exp=2,
                scope="mock scope",
                azp="mock_azp",
                permissions=["organization:management"],
                user_email="mock@mock.com",
                user_full_name="mock full name",
            ),
            "mock_invitation_id_to_remove",
        )
        self.assertTrue(response["invitation"] == "deleted")
        mock_delete_invitation.assert_called_with(
            id="mock_org_id",
            invitation_id="mock_invitation_id_to_remove",
        )
