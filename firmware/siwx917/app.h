/*
 * Application entry points called by the SDK's sl_main.
 */
#ifndef APP_H
#define APP_H

/* Called once after the SDK has initialised the platform and components. */
void app_init(void);

/* Called repeatedly from the main loop. */
void app_process_action(void);

#endif /* APP_H */
