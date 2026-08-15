// 메모리 대역폭 실측 — LLM 토큰 생성 속도 예측용
//
// LLM 추론(batch=1)은 매 토큰마다 모델 가중치 전체를 메모리에서 읽는다.
// 따라서 "대용량 순차 읽기 대역폭"이 tok/s 의 이론 상한을 결정한다.
//
// 빌드: /usr/local/cuda/bin/nvcc -O3 -o membw membw.cu
#include <cstdio>
#include <cuda_runtime.h>

#define CHK(x) do{ cudaError_t e=(x); if(e){printf("CUDA error %s @%d\n",cudaGetErrorString(e),__LINE__);return 1;} }while(0)

// float4 벡터 로드로 순차 읽기 (LLM 가중치 스트리밍과 동일 패턴)
__global__ void readBW(const float4* __restrict__ p, size_t n4, float* out){
    size_t i = blockIdx.x*(size_t)blockDim.x + threadIdx.x;
    size_t stride = (size_t)gridDim.x*blockDim.x;
    float4 acc = make_float4(0,0,0,0);
    for(; i < n4; i += stride){
        float4 v = p[i];
        acc.x += v.x; acc.y += v.y; acc.z += v.z; acc.w += v.w;
    }
    float s = acc.x+acc.y+acc.z+acc.w;
    if (s == 1234.5678f) *out = s;   // 최적화 제거 방지 (실제로는 미실행)
}

int main(){
    size_t BYTES = 2ull<<30;              // 2 GiB — L2(2MB) 훨씬 초과
    size_t n4 = BYTES / sizeof(float4);
    float4 *d; float *out;
    CHK(cudaMalloc(&d, BYTES));
    CHK(cudaMalloc(&out, sizeof(float)));
    CHK(cudaMemset(d, 1, BYTES));

    int blocks = 0, threads = 256;
    cudaOccupancyMaxActiveBlocksPerMultiprocessor(&blocks, readBW, threads, 0);
    cudaDeviceProp prop; cudaGetDeviceProperties(&prop, 0);
    blocks *= prop.multiProcessorCount;

    printf("  버퍼 %.1f GiB / SM %d / 블록 %d x %d스레드\n",
           BYTES/1073741824.0, prop.multiProcessorCount, blocks, threads);

    readBW<<<blocks,threads>>>(d, n4, out);   // 워밍업
    CHK(cudaDeviceSynchronize());

    cudaEvent_t a,b; cudaEventCreate(&a); cudaEventCreate(&b);
    const int IT = 20;
    cudaEventRecord(a);
    for(int i=0;i<IT;i++) readBW<<<blocks,threads>>>(d, n4, out);
    cudaEventRecord(b);
    CHK(cudaDeviceSynchronize());

    float ms=0; cudaEventElapsedTime(&ms,a,b);
    double gbs = (double)BYTES*IT / (ms/1000.0) / 1e9;
    printf("\n  ■ 순차 읽기 대역폭 : %.1f GB/s\n", gbs);
    printf("    (사양치 102.4 GB/s 대비 효율 %.0f%%)\n", gbs/102.4*100);

    // 참고: D2D 복사 (읽기+쓰기 = 트래픽 2배)
    float4* d2; CHK(cudaMalloc(&d2, BYTES));
    cudaEventRecord(a);
    for(int i=0;i<5;i++) cudaMemcpy(d2,d,BYTES,cudaMemcpyDeviceToDevice);
    cudaEventRecord(b);
    CHK(cudaDeviceSynchronize());
    cudaEventElapsedTime(&ms,a,b);
    printf("  ■ D2D 복사 (읽기+쓰기): %.1f GB/s\n", (double)BYTES*2*5/(ms/1000.0)/1e9);

    printf("\n  === 이 대역폭에서 예상되는 LLM 토큰 생성 속도 ===\n");
    printf("  토큰 1개 = 가중치 1회 통독. MoE는 활성 전문가만 읽음.\n\n");
    printf("  %-30s %8s %8s %s\n", "모델 (양자화)", "디스크", "토큰당", "예상 tok/s");
    printf("  ---------------------------------------------------------------\n");
    struct { const char* n; double disk; double perTok; } m[] = {
        // n, 디스크 크기, 토큰당 실제 읽는 양
        {"Qwen3-4B Q4_K_M",            2.5,  2.5},
        {"Qwen3-8B Q4_K_M",            4.9,  4.9},
        {"Qwen3-14B Q4_K_M",           8.5,  8.5},
        {"Qwen3-30B-A3B IQ2_M (MoE)", 10.9,  2.5},  // 30B 중 활성 ~3.3B
    };
    for(auto&x : m){
        double up = gbs/x.perTok;          // 대역폭 기준 이론 상한
        printf("  %-30s %5.1f GB %5.1f GB    %3.0f ~ %.0f\n",
               x.n, x.disk, x.perTok, up*0.6, up*0.8);
    }
    printf("\n  * llama.cpp 실효율 60~80%% 가정 (커널 오버헤드·KV캐시 포함)\n");
    printf("  * MoE는 총 10.9GB를 메모리에 상주시키지만 토큰당 ~2.5GB만 읽으므로\n");
    printf("    14B 덴스보다 크면서 더 빠르다\n");
    return 0;
}
