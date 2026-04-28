// Annealage Pod: CMSIS-DAP core skeleton.
//
// Phase 1 surface: a single `dap_core_start()` entry point. Phase 2
// will vendor in ARM-software CMSIS-DAP `DAP.c` / `SWO.c` (Apache-2.0)
// and wire them to the SPI2 SWD backend and UART1/UHCI SWO pipeline.

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void dap_core_start(void);

#ifdef __cplusplus
}
#endif
