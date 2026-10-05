#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void)
{
    // FD: File Description - 프로세스가 열린 자원을 가리킬 때 사용하는 정수임
    // AF_INET: IPv4 체계 사용
    // SOCKET_STREAM: TCP socket
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);

    if (server_fd == -1) {
	perror("socket");
	return 1;
    }

    printf("Socket created: fd=%d\n", server_fd);

    if (close(server_fd) == -1) {
	perror("close");
	return 1;
    }

    return 0;
}
