#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void)
{
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
