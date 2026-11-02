#ifndef R60ABD1_CLIENT_H
#define R60ABD1_CLIENT_H

#include <linux/r60abd1.h>

struct r60abd1 {
	int fd;
};

int r60abd1_init(struct r60abd1 *radar, const char *path);
int r60abd1_deinit(struct r60abd1 *radar);
int r60abd1_get_mode(struct r60abd1 *radar, enum r60abd1_mode *mode, int timeout_ms);
int r60abd1_set_mode(struct r60abd1 *radar, enum r60abd1_mode mode, int timeout_ms);
int r60abd1_get_data(struct r60abd1 *radar, struct r60abd1_event *event, int timeout_ms);
int r60abd1_get_status(struct r60abd1 *radar, struct r60abd1_status *status);
int r60abd1_get_resp_status(struct r60abd1 *radar, struct r60abd1_event *event, int timeout_ms);
int r60abd1_get_presence(struct r60abd1 *radar, struct r60abd1_event *event, int timeout_ms);
int r60abd1_enable_monitor(struct r60abd1 *radar, unsigned int control, int timeout_ms);
/* Send a read-only query; the reply arrives as a normal event. errno=ENOTTY on older drivers. */
int r60abd1_query(struct r60abd1 *radar, unsigned int control, unsigned int command);

#endif
