#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "r60abd1.h"

static volatile sig_atomic_t running = 1;
static int json_output;
static int queries_supported = 1;

/* 状态类上报只在变化时发送，主动轮询可在丢帧后自愈；波形按1s轮询。 */
#define STATE_POLL_MS 3000
#define WAVE_POLL_MS 1000

static const char *const names[] = {
	[R60ABD1_HEARTBEAT] = "communication-heartbeat",
	[R60ABD1_RESET] = "reset", [R60ABD1_READY] = "ready",
	[R60ABD1_MODEL] = "model", [R60ABD1_PRODUCT_ID] = "product-id",
	[R60ABD1_HARDWARE] = "hardware", [R60ABD1_FIRMWARE] = "firmware",
	[R60ABD1_RANGE] = "range", [R60ABD1_PRESENCE] = "presence",
	[R60ABD1_MOTION] = "motion", [R60ABD1_AMPLITUDE] = "amplitude",
	[R60ABD1_DISTANCE] = "distance-cm", [R60ABD1_RESP_STATUS] = "resp-status",
	[R60ABD1_RESP_RATE] = "resp-per-minute", [R60ABD1_RESP_WAVE] = "resp-wave",
	[R60ABD1_HEART_RATE] = "heart-per-minute", [R60ABD1_HEART_WAVE] = "heart-wave",
	[R60ABD1_BED] = "bed", [R60ABD1_STAGE] = "stage",
	[R60ABD1_AWAKE_TIME] = "awake-minutes", [R60ABD1_LIGHT_TIME] = "light-minutes",
	[R60ABD1_DEEP_TIME] = "deep-minutes", [R60ABD1_SCORE] = "score",
	[R60ABD1_SUMMARY] = "summary", [R60ABD1_ANALYSIS] = "analysis",
	[R60ABD1_SLEEP_ERROR] = "sleep-error", [R60ABD1_QUALITY] = "quality",
	[R60ABD1_STRUGGLE] = "struggle", [R60ABD1_ABSENCE] = "absence",
	[R60ABD1_RAW] = "unknown-frame", [R60ABD1_MODE] = "mode",
	[R60ABD1_PROTOCOL_ERROR] = "protocol-error",
};

static void stop(int signal)
{
	(void)signal;
	running = 0;
}

static void print_event(const struct r60abd1_event *event)
{
	unsigned int i, count = event->count;
	const char *name = event->type < sizeof(names) / sizeof(names[0]) ? names[event->type] : NULL;

	if (event->type == R60ABD1_POSITION)
		return;
	if (event->type == R60ABD1_SUMMARY)
		count = count > 7 ? 7 : count;
	if (event->type == R60ABD1_ANALYSIS)
		count = count > 10 ? 10 : count;
	if (json_output) {
		printf("{\"kind\":\"event\",\"name\":\"%s\",\"mode\":%u,"
		       "\"received_ns\":\"%" PRIu64 "\",\"error\":%u,"
		       "\"control\":%u,\"command\":%u,\"values\":[",
		       name ? name : "unknown-type", event->mode,
		       (uint64_t)event->received_ns, event->error, event->control, event->command);
		for (i = 0; i < count; i++)
			printf("%s%d", i ? "," : "", event->value[i]);
		puts("]}");
		return;
	}
	printf("[%" PRIu64 "ms][mode=%u][%02X/%02X] %s",
	       (uint64_t)event->received_ns / 1000000, event->mode,
	       event->control, event->command, name ? name : "unknown-type");
	for (i = 0; i < count; i++)
		printf(" v%u=%d", i, event->value[i]);
	if ((event->type >= R60ABD1_MODEL && event->type <= R60ABD1_FIRMWARE) ||
	    event->type == R60ABD1_RAW)
		for (i = 0; i < event->length; i++)
			printf(" %02X", event->raw[i]);
	if (event->type == R60ABD1_PROTOCOL_ERROR)
		printf(" errno=%u", event->error);
	putchar('\n');
}

static void sync_resp_status(struct r60abd1 *radar)
{
	struct r60abd1_event event;

	if (r60abd1_get_resp_status(radar, &event, 500) < 0) {
		int error = errno;

		if (!json_output)
			fprintf(stderr, "Respiration status query failed: %s; continuing unsolicited reports.\n",
				strerror(error));
		if (json_output)
			printf("{\"kind\":\"resp-query\",\"ok\":false,\"error\":%d}\n", error);
		return;
	}
	if (json_output)
		puts("{\"kind\":\"resp-query\",\"ok\":true,\"error\":0}");
	print_event(&event);
}

static void sync_presence(struct r60abd1 *radar)
{
	struct r60abd1_event event;

	if (r60abd1_get_presence(radar, &event, 500) < 0) {
		int error = errno;

		if (!json_output)
			fprintf(stderr, "Presence query failed: %s; continuing unsolicited reports.\n",
				strerror(error));
		if (json_output)
			printf("{\"kind\":\"presence-query\",\"ok\":false,\"error\":%d}\n", error);
		return;
	}
	if (json_output)
		puts("{\"kind\":\"presence-query\",\"ok\":true,\"error\":0}");
	print_event(&event);
}

static void query_report(struct r60abd1 *radar, unsigned int control, unsigned int command)
{
	if (queries_supported && r60abd1_query(radar, control, command) < 0 && errno == ENOTTY)
		queries_supported = 0;
}

/* 轮询存在、呼吸状态、运动和范围；后两项需要较新的驱动，旧驱动自动跳过。 */
static void sync_state(struct r60abd1 *radar)
{
	sync_presence(radar);
	sync_resp_status(radar);
	query_report(radar, 0x80, 0x82);
	query_report(radar, 0x07, 0x87);
}

static void enable_monitors(struct r60abd1 *radar, enum r60abd1_mode mode)
{
	static const unsigned int controls[] = {0x80, 0x81, 0x85, 0x84};
	unsigned int i;

	for (i = 0; i < sizeof(controls) / sizeof(controls[0]); i++) {
		int ret, error;

		if (controls[i] == 0x84 && mode != R60ABD1_SLEEP)
			continue;
		if (!running)
			break;
		ret = r60abd1_enable_monitor(radar, controls[i], 1500);
		error = ret < 0 ? errno : 0;
		if (ret < 0)
			fprintf(stderr, "Monitor %02X enable/readback failed: %s; continuing reports.\n",
				controls[i], strerror(error));
		if (json_output)
			printf("{\"kind\":\"monitor-config\",\"control\":%u,"
			       "\"ok\":%s,\"error\":%d}\n",
			       controls[i], ret == 0 ? "true" : "false", error);
	}
}

static int launch_ui(int argc, char **argv)
{
	char executable[4096], script[4096], *slash;
	char *args[8];
	ssize_t length;
	int i;

	length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
	if (length < 0 || (size_t)length >= sizeof(executable) - 1) {
		perror("locate executable");
		return 1;
	}
	executable[length] = '\0';
	strcpy(script, executable);
	slash = strrchr(script, '/');
	if (!slash || (size_t)(slash - script) + sizeof("/ui_server.py") > sizeof(script)) {
		fprintf(stderr, "Cannot locate ui_server.py next to executable.\n");
		return 1;
	}
	strcpy(slash + 1, "ui_server.py");
	args[0] = "python3";
	args[1] = script;
	args[2] = "--collector";
	args[3] = executable;
	for (i = 1; i < argc; i++)
		args[i + 3] = argv[i];
	args[argc + 3] = NULL;
	execvp(args[0], args);
	perror("launch local UI (requires python3 and ui_server.py)");
	return 1;
}

static int print_status(struct r60abd1 *radar, uint64_t initial_dropped)
{
	struct r60abd1_status status;

	if (r60abd1_get_status(radar, &status) < 0)
		return -1;
	printf("{\"kind\":\"status\",\"frames\":%" PRIu64 ",\"protocol_errors\":%" PRIu64
	       ",\"dropped_total\":%" PRIu64 ",\"dropped_this_run\":%" PRIu64 "}\n",
	       (uint64_t)status.frames, (uint64_t)status.errors,
	       (uint64_t)status.dropped, (uint64_t)status.dropped - initial_dropped);
	return 0;
}

int main(int argc, char **argv)
{
	struct r60abd1 radar = {.fd = -1};
	struct r60abd1_status status;
	struct r60abd1_event event;
	struct sigaction action = {.sa_handler = stop};
	enum r60abd1_mode mode;
	uint64_t initial_dropped;
	int64_t next_state_ms, next_wave_ms, now_ms;
	struct timespec epoch, now, next_status;
	int ret, result = 0;

	if (argc >= 2 && (!strcmp(argv[1], "--json") || !strcmp(argv[1], "--terminal"))) {
		json_output = !strcmp(argv[1], "--json");
		argc--;
		argv++;
	} else {
		if (argc > 3) {
			fprintf(stderr, "Usage: demo-mmwave-human-sensing [device [realtime|sleep]]\n");
			return 1;
		}
		return launch_ui(argc, argv);
	}
	if (argc > 3 || (argc == 3 && strcmp(argv[2], "realtime") && strcmp(argv[2], "sleep"))) {
		fprintf(stderr, "Usage: %s [device [realtime|sleep]]\n", argv[0]);
		return 1;
	}
	if (sigemptyset(&action.sa_mask) < 0 || sigaction(SIGINT, &action, NULL) < 0 ||
	    sigaction(SIGTERM, &action, NULL) < 0) {
		perror("signal");
		return 1;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (r60abd1_init(&radar, argc >= 2 ? argv[1] : NULL) < 0) {
		perror("open radar");
		return 1;
	}
	if (r60abd1_get_status(&radar, &status) < 0) {
		perror("get status");
		result = 1;
		goto out;
	}
	initial_dropped = status.dropped;
	if (r60abd1_get_mode(&radar, &mode, 5000) < 0) {
		perror("get mode");
		result = 1;
		goto out;
	}
	if (!json_output)
		printf("Current mode=%u; allow >=30s after radar power-on.\n", mode);
	if (argc == 3) {
		enum r60abd1_mode target = !strcmp(argv[2], "sleep") ? R60ABD1_SLEEP : R60ABD1_REALTIME;

		if (mode != target && r60abd1_set_mode(&radar, target, 5000) < 0) {
			perror("set mode");
			result = 1;
			goto out;
		}
		mode = target;
	}
	enable_monitors(&radar, mode);
	if (!running)
		goto out;
	if (clock_gettime(CLOCK_REALTIME, &epoch) < 0 ||
	    clock_gettime(CLOCK_MONOTONIC, &next_status) < 0) {
		perror("clock");
		result = 1;
		goto out;
	}
	if (json_output) {
		printf("{\"kind\":\"session\",\"mode\":%u,\"after_ns\":\"%" PRIu64 "\"}\n",
		       mode, (uint64_t)epoch.tv_sec * 1000000000 + (uint64_t)epoch.tv_nsec);
		if (print_status(&radar, initial_dropped) < 0) {
			perror("status");
			result = 1;
			goto out;
		}
	}
	sync_presence(&radar);
	sync_resp_status(&radar);
	next_state_ms = (int64_t)next_status.tv_sec * 1000 + next_status.tv_nsec / 1000000;
	next_wave_ms = next_state_ms;
	next_state_ms += STATE_POLL_MS;
	while (running) {
		if (json_output) {
			if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
				perror("clock");
				result = 1;
				break;
			}
			if (now.tv_sec >= next_status.tv_sec + 2) {
				if (print_status(&radar, initial_dropped) < 0) {
					perror("status");
					result = 1;
					break;
				}
				next_status = now;
			}
			now_ms = (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
			if (now_ms >= next_state_ms) {
				sync_state(&radar);
				next_state_ms = now_ms + STATE_POLL_MS;
			}
			if (mode == R60ABD1_REALTIME && now_ms >= next_wave_ms) {
				query_report(&radar, 0x81, 0x85);
				query_report(&radar, 0x85, 0x85);
				next_wave_ms = now_ms + WAVE_POLL_MS;
			}
		}
		ret = r60abd1_get_data(&radar, &event, 200);
		if (ret == 0 || (ret < 0 && errno == EINTR))
			continue;
		if (ret < 0) {
			if (errno == EOVERFLOW) {
				if (!json_output)
					fprintf(stderr, "Event queue overflow; older events were lost.\n");
				if (json_output)
					puts("{\"kind\":\"data-loss\"}");
				sync_state(&radar);
				continue;
			}
			perror("read radar");
			result = 1;
			break;
		}
		print_event(&event);
		if (event.type == R60ABD1_RESET) {
			fprintf(stderr, "Radar reset; wait for startup and run again.\n");
			result = 1;
			break;
		}
	}
	if (r60abd1_get_status(&radar, &status) < 0) {
		perror("get final status");
		result = 1;
	} else if (!json_output) {
		printf("frames=%" PRIu64 " protocol_errors=%" PRIu64
		       " dropped_total=%" PRIu64 " dropped_this_run=%" PRIu64 "\n",
		       (uint64_t)status.frames, (uint64_t)status.errors,
		       (uint64_t)status.dropped, (uint64_t)status.dropped - initial_dropped);
	}
out:
	if (r60abd1_deinit(&radar) < 0) {
		perror("close radar");
		result = 1;
	}
	return result;
}
