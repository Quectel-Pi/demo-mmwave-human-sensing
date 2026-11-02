#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include "r60abd1.h"

_Static_assert(sizeof(struct r60abd1_event) == 4176, "event ABI mismatch");
_Static_assert(sizeof(struct r60abd1_status) == 64, "status ABI mismatch");
_Static_assert(sizeof(struct r60abd1_state_arg) == 24, "state query ABI mismatch");
_Static_assert(sizeof(struct r60abd1_monitor_arg) == 8, "monitor ABI mismatch");
_Static_assert(sizeof(struct r60abd1_query_arg) == 8, "query ABI mismatch");

static int valid(struct r60abd1 *radar)
{
	if (!radar || radar->fd < 0) {
		errno = EBADF;
		return 0;
	}
	return 1;
}

int r60abd1_get_status(struct r60abd1 *radar, struct r60abd1_status *status)
{
	if (!valid(radar))
		return -1;
	if (!status) {
		errno = EINVAL;
		return -1;
	}
	if (ioctl(radar->fd, R60ABD1_GET_STATUS, status) < 0)
		return -1;
	if (status->version != R60ABD1_VERSION || status->event_size != sizeof(struct r60abd1_event)) {
		errno = EPROTO;
		return -1;
	}
	return 0;
}

int r60abd1_init(struct r60abd1 *radar, const char *path)
{
	struct r60abd1_status status;
	int error;

	if (!radar || radar->fd != -1) {
		errno = EINVAL;
		return -1;
	}
	radar->fd = open(path ? path : R60ABD1_DEVICE, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	if (radar->fd < 0)
		return -1;
	if (r60abd1_get_status(radar, &status) == 0) {
		if (status.connected)
			return 0;
		errno = ENODEV;
	}
	error = errno;
	close(radar->fd);
	radar->fd = -1;
	errno = error;
	return -1;
}

int r60abd1_deinit(struct r60abd1 *radar)
{
	int ret;

	if (!radar) {
		errno = EINVAL;
		return -1;
	}
	if (radar->fd < 0)
		return 0;
	ret = close(radar->fd);
	radar->fd = -1;
	return ret;
}

static int mode_request(struct r60abd1 *radar, enum r60abd1_mode *mode, int timeout_ms, int set)
{
	struct r60abd1_mode_arg arg;

	if (!valid(radar))
		return -1;
	if (!mode || timeout_ms < 1 || timeout_ms > 60000 ||
	    (set && *mode != R60ABD1_REALTIME && *mode != R60ABD1_SLEEP)) {
		errno = EINVAL;
		return -1;
	}
	arg.mode = set ? *mode : R60ABD1_UNKNOWN;
	arg.timeout_ms = (unsigned int)timeout_ms;
	if (ioctl(radar->fd, set ? R60ABD1_SET_MODE : R60ABD1_GET_MODE, &arg) < 0)
		return -1;
	if (!set) {
		if (arg.mode > R60ABD1_SLEEP) {
			errno = EPROTO;
			return -1;
		}
		*mode = (enum r60abd1_mode)arg.mode;
	}
	return 0;
}

int r60abd1_get_mode(struct r60abd1 *radar, enum r60abd1_mode *mode, int timeout_ms)
{
	return mode_request(radar, mode, timeout_ms, 0);
}

int r60abd1_set_mode(struct r60abd1 *radar, enum r60abd1_mode mode, int timeout_ms)
{
	return mode_request(radar, &mode, timeout_ms, 1);
}

static int get_state(struct r60abd1 *radar, struct r60abd1_event *event,
		     unsigned int type, int timeout_ms)
{
	struct r60abd1_state_arg arg = {.type = type};

	if (!valid(radar))
		return -1;
	if (!event || timeout_ms < 1 || timeout_ms > 60000) {
		errno = EINVAL;
		return -1;
	}
	arg.timeout_ms = (unsigned int)timeout_ms;
	if (ioctl(radar->fd, R60ABD1_GET_STATE, &arg) < 0)
		return -1;
	if ((type == R60ABD1_PRESENCE && (arg.value < 0 || arg.value > 1)) ||
	    (type == R60ABD1_RESP_STATUS && (arg.value < 1 || arg.value > 4)) ||
	    arg.mode > R60ABD1_SLEEP || !arg.received_ns) {
		errno = EPROTO;
		return -1;
	}
	memset(event, 0, sizeof(*event));
	event->version = R60ABD1_VERSION;
	event->type = type;
	event->count = 1;
	event->value[0] = arg.value;
	event->received_ns = arg.received_ns;
	event->mode = arg.mode;
	event->control = type == R60ABD1_PRESENCE ? 0x80 : 0x81;
	event->command = 0x81;
	return 0;
}

int r60abd1_get_resp_status(struct r60abd1 *radar, struct r60abd1_event *event, int timeout_ms)
{
	return get_state(radar, event, R60ABD1_RESP_STATUS, timeout_ms);
}

int r60abd1_get_presence(struct r60abd1 *radar, struct r60abd1_event *event, int timeout_ms)
{
	return get_state(radar, event, R60ABD1_PRESENCE, timeout_ms);
}

int r60abd1_enable_monitor(struct r60abd1 *radar, unsigned int control, int timeout_ms)
{
	struct r60abd1_monitor_arg arg;

	if (!valid(radar))
		return -1;
	if (timeout_ms < 1 || timeout_ms > 60000 ||
	    (control != 0x80 && control != 0x81 && control != 0x84 && control != 0x85)) {
		errno = EINVAL;
		return -1;
	}
	arg.control = control;
	arg.timeout_ms = (unsigned int)timeout_ms;
	return ioctl(radar->fd, R60ABD1_ENABLE_MONITOR, &arg) < 0 ? -1 : 0;
}

int r60abd1_query(struct r60abd1 *radar, unsigned int control, unsigned int command)
{
	struct r60abd1_query_arg arg = {.control = control, .command = command};

	if (!valid(radar))
		return -1;
	return ioctl(radar->fd, R60ABD1_QUERY, &arg) < 0 ? -1 : 0;
}

int r60abd1_get_data(struct r60abd1 *radar, struct r60abd1_event *event, int timeout_ms)
{
	struct pollfd wait;
	ssize_t length;
	int ret;

	if (!valid(radar))
		return -1;
	if (!event || timeout_ms < 0 || timeout_ms > 60000) {
		errno = EINVAL;
		return -1;
	}
	wait = (struct pollfd){.fd = radar->fd, .events = POLLIN};
	ret = poll(&wait, 1, timeout_ms);
	if (ret <= 0)
		return ret;
	length = read(radar->fd, event, sizeof(*event));
	if (length < 0)
		return errno == EAGAIN || errno == EWOULDBLOCK ? 0 : -1;
	if (length != (ssize_t)sizeof(*event) || event->version != R60ABD1_VERSION ||
	    event->count > R60ABD1_MAX_VALUES || event->length > R60ABD1_MAX_PAYLOAD) {
		errno = length == 0 ? ENODEV : EPROTO;
		return -1;
	}
	return 1;
}
