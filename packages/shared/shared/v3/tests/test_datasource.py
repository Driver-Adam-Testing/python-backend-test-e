from shared.v3.utils.datasource import DataSource


def test_data_source_from_version_node_ids() -> None:
    """
    Basic test for DataSource.from_version_node_ids
    Ensures we can create a DataSource from a list of version_node_ids and an organization_id.
    Note: This will fail at runtime with DB validation, but we're just testing the interface.
    """
    version_node_ids = []
    organization_id = "test_org"
    user_id = "test_user"
    ds = DataSource.from_version_node_ids(
        version_node_ids=version_node_ids,
        organization_id=organization_id,
        user_id=user_id,
    )

    assert ds.version_node_ids == version_node_ids
    assert ds.organization_id == organization_id
    assert ds.user_id == user_id
