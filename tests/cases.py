"""One case per client method: how it is called and what it must send and return."""

from __future__ import annotations

from dataclasses import dataclass, field

from gcpapi_sync import ActivityFile, models
from tests.helpers import Reply, zip_archive

DAY = "2026-10-21"
START = "2026-10-15"
END = "2026-10-21"
ACTIVITY_ID = 24799472038
ZIP = zip_archive({f"{ACTIVITY_ID}_ACTIVITY.fit": b"\x0e\x10.FIT-data"})


@dataclass(frozen=True)
class Case:
    """A client method call, the request it must make and the result type it must return."""

    method: str
    path: str
    reply: Reply
    result: type
    args: tuple[object, ...] = ()
    kwargs: dict[str, object] = field(default_factory=dict)
    params: tuple[tuple[str, str], ...] = ()
    item: type | None = None
    accept: str = "application/json"

    @property
    def id(self) -> str:
        """Readable pytest id."""
        return self.method


CASES: tuple[Case, ...] = (
    Case(
        "get_activity",
        f"/activity-service/activity/{ACTIVITY_ID}",
        Reply.fixture("activity"),
        models.ActivitySummary,
        args=(ACTIVITY_ID,),
    ),
    Case(
        "get_activity_workouts",
        f"/activity-service/activity/{ACTIVITY_ID}/workouts",
        Reply.json([{"workoutId": 1, "workoutName": "Intervals"}]),
        list,
        args=(ACTIVITY_ID,),
        item=models.ActivityWorkout,
    ),
    Case(
        "search_activities",
        "/activitylist-service/activities/search/activities",
        Reply.fixture("search_activities"),
        list,
        kwargs={"limit": 3, "activity_type": "running", "start_date": START},
        params=(("start", "0"), ("limit", "3"), ("startDate", START), ("activityType", "running")),
        item=models.SummarizedActivity,
    ),
    Case(
        "download_activity",
        f"/download-service/files/activity/{ACTIVITY_ID}",
        Reply(body=ZIP, headers=(("Content-Type", "application/x-zip-compressed"),)),
        ActivityFile,
        args=(ACTIVITY_ID,),
        accept="application/x-zip-compressed",
    ),
    Case(
        "download_activity_zip",
        f"/download-service/files/activity/{ACTIVITY_ID}",
        Reply(body=ZIP, headers=(("Content-Type", "application/x-zip-compressed"),)),
        bytes,
        args=(ACTIVITY_ID,),
        accept="application/x-zip-compressed",
    ),
    Case(
        "get_heart_rate_zones",
        "/biometric-service/heartRateZones/v2",
        Reply.fixture("heart_rate_zones"),
        list,
        kwargs={"generate_defaults": False},
        params=(("generateDefaults", "false"),),
        item=models.HeartRateZones,
    ),
    Case(
        "get_fitness_age",
        f"/fitnessage-service/fitnessage/v2/{DAY}",
        Reply.fixture("fitness_age"),
        models.FitnessAge,
        args=(DAY,),
        params=(("fullHistoryScan", "true"),),
    ),
    Case(
        "get_effective_weight_goals",
        f"/goal-service/goal/user/effective/weightgoal/{START}/{END}",
        Reply.fixture("effective_weight_goals"),
        models.UserGoalsRange,
        args=(START, END),
    ),
    Case(
        "get_daily_hrv",
        f"/hrv-service/hrv/{DAY}",
        Reply.fixture("daily_hrv"),
        models.HrvChart,
        args=(DAY,),
    ),
    Case(
        "get_daily_hrv_summaries",
        f"/hrv-service/hrv/daily/{START}/{END}",
        Reply.fixture("daily_hrv_summaries"),
        list,
        args=(START, END),
        item=models.HrvSummary,
    ),
    Case(
        "get_daily_food_logs",
        f"/nutrition-service/food/logs/{DAY}",
        Reply.fixture("daily_food_logs"),
        models.DailyNutritionSummary,
        args=(DAY,),
        kwargs={"include_media": False, "meal_id": 3},
        params=(
            ("includeGoals", "true"),
            ("includeNutrition", "true"),
            ("includeMedia", "false"),
            ("mealId", "3"),
        ),
    ),
    Case(
        "get_menstrual_cycle_summary",
        f"/periodichealth-service/menstrualcycle/summary/{DAY}",
        Reply(status=204),
        type(None),
        args=(DAY,),
    ),
    Case(
        "get_menstrual_cycle_day_view",
        f"/periodichealth-service/menstrualcycle/dayview/{DAY}",
        Reply.json({"calendarDate": DAY}),
        models.DetailedMenstrualCycleDay,
        args=(DAY,),
        kwargs={"today": DAY, "include_analysis": True},
        params=(
            ("todayDate", DAY),
            ("reducedResponse", "false"),
            ("skipbm", "false"),
            ("includeAnalysis", "true"),
        ),
    ),
    Case(
        "get_daily_sleep",
        "/sleep-service/sleep/dailySleep",
        Reply.fixture("daily_sleep"),
        models.DailySleep,
        args=(DAY,),
        params=(("date", DAY),),
    ),
    Case(
        "get_daily_sleep_summaries",
        "/sleep-service/sleep/dailySleepSummariesByDate",
        Reply.fixture("daily_sleep_summaries"),
        list,
        args=(START, END),
        params=(("startDate", START), ("endDate", END)),
        item=models.DailySleep,
    ),
    Case(
        "get_user_profile_settings",
        "/userprofile-service/userprofile/settings",
        Reply.fixture("user_profile_settings"),
        models.UserSettings,
    ),
    Case(
        "get_daily_hydration",
        f"/usersummary-service/stats/hydration/daily/{START}/{END}",
        Reply.json([{"calendarDate": END, "valueInML": 1500.0, "goalInML": 2500.0}]),
        list,
        args=(START, END),
        item=models.UserDailyHydration,
    ),
    Case(
        "get_daily_steps",
        f"/usersummary-service/stats/steps/daily/{START}/{END}",
        Reply.fixture("daily_steps"),
        list,
        args=(START, END),
        item=models.StepsDailyStat,
    ),
    Case(
        "get_daily_stress",
        f"/usersummary-service/stats/stress/daily/{START}/{END}",
        Reply.fixture("daily_stress"),
        list,
        args=(START, END),
        item=models.MultiValueWellnessChartItem,
    ),
    Case(
        "get_daily_summary",
        "/usersummary-service/usersummary/daily",
        Reply.fixture("daily_summary"),
        models.UserDailySummary,
        args=(DAY,),
        params=(("calendarDate", DAY),),
    ),
    Case(
        "get_daily_summaries",
        "/usersummary-service/v2/dailysummaries",
        Reply.fixture("daily_summaries"),
        list,
        args=(START, END),
        params=(("startDate", START), ("endDate", END)),
        item=models.UserDailySummaryV2,
    ),
    Case(
        "get_weight_entries",
        f"/weight-service/weight/all/{START}/{END}",
        Reply.json([{"calendarDate": END, "weight": 70000.0, "date": 1792621739000}]),
        list,
        args=(START, END),
        item=models.DateWeight,
    ),
    Case(
        "get_weight_range",
        f"/weight-service/weight/range/{START}/{END}",
        Reply.fixture("weight_range"),
        models.WeightRange,
        args=(START, END),
        kwargs={"include_all": True},
        params=(("includeAll", "true"),),
    ),
    Case(
        "get_daily_body_battery",
        "/wellness-service/wellness/bodyBattery/reports/daily",
        Reply.fixture("daily_body_battery"),
        list,
        args=(START, END),
        kwargs={"chunks": 4},
        params=(("startDate", START), ("endDate", END), ("chunks", "4")),
        item=models.BodyBatteryDailyReport,
    ),
    Case(
        "get_daily_spo2",
        f"/wellness-service/wellness/daily/spo2acclimation/{DAY}",
        Reply.fixture("daily_spo2"),
        models.DailySpO2AndAcclimation,
        args=(DAY,),
    ),
    Case(
        "get_daily_heart_rate",
        "/wellness-service/wellness/dailyHeartRate",
        Reply.fixture("daily_heart_rate"),
        models.DailyHeartRate,
        args=(DAY,),
        params=(("date", DAY),),
    ),
    Case(
        "get_wellness_activity_summaries",
        f"/wellnessactivity-service/activity/summary/range/{START}/{END}",
        Reply.fixture("wellness_activity_summaries"),
        list,
        args=(START, END),
        item=models.WellnessActivitySummary,
    ),
    Case(
        "get_json",
        "/userprofile-service/userprofile/settings",
        Reply.fixture("user_profile_settings"),
        dict,
        args=("/userprofile-service/userprofile/settings", [("a", "1")]),
        params=(("a", "1"),),
    ),
)

# Methods that are not plain endpoint wrappers and are tested separately.
NOT_ENDPOINTS = frozenset({"close", "get_access_token", "iter_activities", "can_refresh"})
