"""M-05：商品卡片必须从 RunItem.suggested_hs 读专家建议，而不是 result 里的那个字段。"""
from django.test import override_settings

from apps.reviews.serializers import item_to_card
from apps.runs.models import Run, RunItem
from tests.p10.factories import SubmissionFixture

HS_EXPERT = "8544422000"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExpertSuggestedHsTests(SubmissionFixture):
    def _expert_run(self):
        run = Run.objects.create(
            task=self.task, version=self.version, kind=Run.Kind.EXPERT_CHECK,
            submission=self.submission, is_current=True,
        )
        RunItem.objects.create(
            run=run, item=self.item_a, status="succeeded",
            suggested_hs=HS_EXPERT, result={"note": "合成结果：result 内没有 suggested_hs"},
        )
        return run

    def test_card_reads_expert_suggested_hs_from_run_item(self):
        run = self._expert_run()
        card = item_to_card(self.expert, self.item_a)
        self.assertEqual(card.expert_suggested_hs.value, HS_EXPERT)
        self.assertEqual(card.expert_suggested_hs.source_id, str(run.pk))

    def test_card_without_expert_run_keeps_null(self):
        card = item_to_card(self.expert, self.item_a)
        self.assertIsNone(card.expert_suggested_hs.value)
