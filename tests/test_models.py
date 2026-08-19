from flipp_dl.models import Category, Issue, Publication

SAMPLE_PUBLICATION = {
    "customPublicationCode": "KA",
    "name": "Kalle Anka & Co",
    "categories": [
        {"id": 52, "name": "Serietidningar"},
        {"id": 7, "name": "Barn"},
    ],
    "issues": [
        {
            "customIssueCode": "KA-2024-01",
            "issueName": "Nr 1",
            "issueDate": "2024-01-01",
        },
        {
            "customIssueCode": "KA-2024-02",
            "issueName": "Nr 2",
            "issueDate": "2024-01-15",
        },
    ],
}


def test_issue_from_api_reads_fields():
    issue = Issue.from_api(SAMPLE_PUBLICATION["issues"][0])
    assert issue.custom_code == "KA-2024-01"
    assert issue.issue_name == "Nr 1"
    assert issue.issue_date == "2024-01-01"


def test_issue_from_api_tolerates_missing_optionals():
    issue = Issue.from_api({"customIssueCode": "X"})
    assert issue.custom_code == "X"
    assert issue.issue_name == ""
    assert issue.issue_date == ""


def test_publication_from_api_parses_nested_fields():
    pub = Publication.from_api(SAMPLE_PUBLICATION)
    assert pub.custom_code == "KA"
    assert pub.name == "Kalle Anka & Co"
    assert pub.num_issues == 2
    assert pub.categories == [
        Category(id=52, name="Serietidningar"),
        Category(id=7, name="Barn"),
    ]


def test_has_category():
    pub = Publication.from_api(SAMPLE_PUBLICATION)
    assert pub.has_category(52)
    assert pub.has_category(7)
    assert not pub.has_category(999)


def test_publication_from_api_with_no_issues():
    pub = Publication.from_api(
        {
            "customPublicationCode": "X",
            "name": "Empty",
            "categories": [],
            "issues": [],
        }
    )
    assert pub.num_issues == 0
    assert pub.categories == []


def test_num_downloaded_counts_only_done_issues():
    from flipp_dl.db.models import DbIssue, DbPublication, IssueStatus

    pub = DbPublication(custom_code="KA", name="Kalle Anka")
    pub.issues = [
        DbIssue(custom_code="a", issue_name="Nr 1", status=IssueStatus.DONE),
        DbIssue(custom_code="b", issue_name="Nr 2", status=IssueStatus.QUEUED),
        DbIssue(custom_code="c", issue_name="Nr 3", status=IssueStatus.ERROR),
    ]
    assert pub.num_issues == 3
    assert pub.num_downloaded == 1
