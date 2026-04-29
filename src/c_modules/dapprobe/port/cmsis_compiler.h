/* Annealage Pod: CMSIS-compiler shim for xtensa GCC.
 *
 * The vendored ARM-software/CMSIS-DAP `DAP.h` includes <cmsis_compiler.h>
 * for the `__STATIC_INLINE`, `__STATIC_FORCEINLINE`, `__WEAK`, `__NOP`,
 * and `__ASM` macros. The full CMSIS chain lives at
 * src/micropython/lib/cmsis/inc/, but pulling that in on xtensa GCC drags
 * Cortex-M intrinsics that do not exist on the ESP32-S3. This file
 * defines just the minimum the vendored CMSIS-DAP sources reference,
 * mapped to GCC builtins where applicable and to no-ops where not.
 *
 * Scope: only the SWD/SWO protocol layer compiles against this. SW_DP.c
 * (which uses tight inline assembly delays) is replaced by
 * port/swd_glue.c, so the assembly path is never exercised on this
 * target.
 */

#ifndef MPY_POD_DAPPROBE_CMSIS_COMPILER_H
#define MPY_POD_DAPPROBE_CMSIS_COMPILER_H

#ifndef __STATIC_INLINE
#define __STATIC_INLINE static inline
#endif

#ifndef __STATIC_FORCEINLINE
#define __STATIC_FORCEINLINE __attribute__((always_inline)) static inline
#endif

#ifndef __WEAK
#define __WEAK __attribute__((weak))
#endif

#ifndef __NOP
#define __NOP() __asm__ __volatile__ ("nop")
#endif

#ifndef __ASM
#define __ASM __asm__
#endif

#endif /* MPY_POD_DAPPROBE_CMSIS_COMPILER_H */
